import asyncio
from base64 import b64encode
from dataclasses import dataclass, field
from datetime import datetime as dt
import logging
from typing import Literal, Optional
import re
from collections import OrderedDict

import discord
import httpx
from openai import AsyncOpenAI
import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
)

VISION_MODEL_TAGS = ("gpt-4o", "claude-3", "gemini", "pixtral", "llava", "vision", "vl")
PROVIDERS_SUPPORTING_USERNAMES = ("openai", "x-ai")

ALLOWED_FILE_TYPES = ("image", "text")
ALLOWED_CHANNEL_TYPES = (discord.ChannelType.text, discord.ChannelType.public_thread, discord.ChannelType.private_thread, discord.ChannelType.private)

EMBED_COLOR_COMPLETE = discord.Color.dark_green()
EMBED_COLOR_INCOMPLETE = discord.Color.orange()

STREAMING_INDICATOR = " ⚪"
EDIT_DELAY_SECONDS = 1

MAX_MESSAGE_NODES = 100

PROMPT_PATTERN = re.compile(r'<prompt>(.*?)</prompt>',re.DOTALL)


def get_config(filename="config.yaml"):
    with open(filename, "r") as file:
        return yaml.safe_load(file)


cfg = get_config()

if client_id := cfg["client_id"]:
    logging.info(f"\n\nBOT INVITE URL:\nhttps://discord.com/api/oauth2/authorize?client_id={client_id}&permissions=412317273088&scope=bot\n")

intents = discord.Intents.default()
intents.message_content = True
activity = discord.CustomActivity(name=(cfg["status_message"] or "github.com/jakobdylanc/llmcord")[:128])
discord_client = discord.Client(intents=intents, activity=activity)

httpx_client = httpx.AsyncClient()

msg_nodes = OrderedDict()
last_task_time = None


@dataclass
class MsgNode:
    text: Optional[str] = None
    images: list = field(default_factory=list)

    role: Literal["user", "assistant"] = "assistant"
    user_id: Optional[int] = None

    next_msg: Optional[discord.Message] = None

    has_bad_attachments: bool = False
    fetch_next_failed: bool = False

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    username: Optional[str] = None


@discord_client.event
async def on_message(new_msg):
    global msg_nodes, last_task_time, cfg

    is_dm: bool = new_msg.channel.type == discord.ChannelType.private

    if new_msg.author.bot or new_msg.channel.type not in ALLOWED_CHANNEL_TYPES or (not is_dm and discord_client.user not in new_msg.mentions):
        return


    allow_dms: bool = cfg["allow_dms"]
    allowed_channel_ids = cfg["allowed_channel_ids"]
    allowed_role_ids = cfg["allowed_role_ids"]

    if (
        (is_dm and not allow_dms)
        or (allowed_channel_ids and not is_dm and not any(id in allowed_channel_ids for id in (new_msg.channel.id, getattr(new_msg.channel, "parent_id", None))))
        or (allowed_role_ids and not any(role.id in allowed_role_ids for role in getattr(new_msg.author, "roles", [])))
    ):
        return

    #the resolving here is related to selecting models and api keys
    provider, model = resolve_config_user(new_msg,cfg["model"])[0][0].split("/",1)
    provider_cfg = resolve_config_user(new_msg,cfg["providers"])[0][0]

    base_url = provider_cfg[provider]["base_url"]
    api_key = provider_cfg[provider].get("api_key", "sk-no-key-required")
    openai_client = AsyncOpenAI(base_url=base_url, api_key=api_key)

    accept_images: bool = any(x in model.lower() for x in VISION_MODEL_TAGS)
    accept_usernames: bool = any(x in provider.lower() for x in PROVIDERS_SUPPORTING_USERNAMES)

    max_text = cfg["max_text"]
    max_images = cfg["max_images"] if accept_images else 0
    max_messages = cfg["max_messages"]

    use_plain_responses: bool = cfg["use_plain_responses"]
    max_message_length = 2000 if use_plain_responses else (4096 - len(STREAMING_INDICATOR))

    # Build message chain and set user warnings
    messages = []
    user_warnings = set()
    curr_msg = new_msg
    while curr_msg != None and len(messages) < max_messages:
        curr_node = msg_nodes.setdefault(curr_msg.id, MsgNode())

        async with curr_node.lock:
            if curr_node.text == None:
                good_attachments = {type: [att for att in curr_msg.attachments if att.content_type and type in att.content_type] for type in ALLOWED_FILE_TYPES}

                curr_node.text = "\n".join(
                    ([curr_msg.content] if curr_msg.content else [])
                    + [embed.description for embed in curr_msg.embeds if embed.description]
                    + [(await httpx_client.get(att.url)).text for att in good_attachments["text"]]
                )
                if curr_node.text.startswith(discord_client.user.mention):
                    curr_node.text = curr_node.text.replace(discord_client.user.mention, "", 1).lstrip()

                curr_node.images = [
                    dict(type="image_url", image_url=dict(url=f"data:{att.content_type};base64,{b64encode((await httpx_client.get(att.url)).content).decode('utf-8')}"))
                    for att in good_attachments["image"]
                ]

                curr_node.role = "assistant" if curr_msg.author == discord_client.user else "user"

                curr_node.user_id = curr_msg.author.id if curr_node.role == "user" else None

                curr_node.has_bad_attachments = len(curr_msg.attachments) > sum(len(att_list) for att_list in good_attachments.values())

                curr_node.username = getattr(curr_msg.author,'display_name',None)

                try:
                    if (
                        curr_msg.reference == None
                        and discord_client.user.mention not in curr_msg.content
                        and (prev_msg_in_channel := ([m async for m in curr_msg.channel.history(before=curr_msg, limit=1)] or [None])[0])
                        and any(prev_msg_in_channel.type == type for type in (discord.MessageType.default, discord.MessageType.reply))
                        and prev_msg_in_channel.author == (discord_client.user if curr_msg.channel.type == discord.ChannelType.private else curr_msg.author)
                    ):
                        curr_node.next_msg = prev_msg_in_channel
                    else:
                        next_is_thread_parent: bool = curr_msg.reference == None and curr_msg.channel.type == discord.ChannelType.public_thread
                        if next_msg_id := curr_msg.channel.id if next_is_thread_parent else getattr(curr_msg.reference, "message_id", None):
                            if next_is_thread_parent:
                                curr_node.next_msg = curr_msg.channel.starter_message or await curr_msg.channel.parent.fetch_message(next_msg_id)
                            else:
                                curr_node.next_msg = curr_msg.reference.cached_message or await curr_msg.channel.fetch_message(next_msg_id)

                except (discord.NotFound, discord.HTTPException, AttributeError):
                    logging.exception("Error fetching next message in the chain")
                    curr_node.fetch_next_failed = True

            if curr_node.images[:max_images]:
                content = ([dict(type="text", text=curr_node.text[:max_text])] if curr_node.text[:max_text] else []) + curr_node.images[:max_images]
            else:
                content = curr_node.text[:max_text]

            if content != "":
                message = dict(content=content, role=curr_node.role)
                # if curr_node.username == discord_client.user.display_name:
                #     if isinstance(message['content'],str):
                #         # logging.info(f'message["content"]: {message["content"]}')
                #         # message['content'] = model_pattern.sub('',message['content'])
                #         # pattern = f'^\\[From:{discord_client.user.display_name}\\]\n'
                #         # logging.info(f'pattern: {pattern}')
                #         # message['content'] = re.sub(pattern,'',message['content'])
                #     else:
                #         if 'text' in message['content'][0]:
                #             # logging.info(f'message["content"][0]["text"]: {message["content"][0]["text"]}')
                #             message['content'][0]['text'] = model_pattern.sub('',message['content'][0]['text'])
                #             # message['content'][0]['text'] = re.sub(f'^\\[From:{discord_client.user.mention}\\]\n','',message['content'][0]['text'])

                if accept_usernames and curr_node.user_id != None:
                    message["name"] = str(curr_node.user_id)
                elif curr_node.username is not None and curr_node.username != discord_client.user.display_name:
                    if isinstance(message['content'],str):
                        # logging.info(f'Message {message}')
                        message['content'] = '[From:'+curr_node.username+']\n'+message['content']
                        #logging.info(f'Inserted username {curr_node.username}: {message}')

                    else:
                        if 'text' in message['content'][0]:
                            # logging.info(f'Message content 0 keys: {message["content"][0].keys()}')
                            message['content'][0]['text'] = '[From:'+curr_node.username+']\n'+message['content'][0]['text']
                            #logging.info(f'Inserted username {curr_node.username} for first message of {len(message["content"])}')
                        else:
                            message['content'].insert(0,dict(type='text',text='[From:'+curr_node.username+']'))
                            #logging.info(f'Inserted username {curr_node.username} as new message of {len(message["content"])}')

                messages.append(message)

            if len(curr_node.text) > max_text:
                user_warnings.add(f"⚠️ Max {max_text:,} characters per message")
            if len(curr_node.images) > max_images:
                user_warnings.add(f"⚠️ Max {max_images} image{'' if max_images == 1 else 's'} per message" if max_images > 0 else "⚠️ Can't see images")
            if curr_node.has_bad_attachments:
                user_warnings.add("⚠️ Unsupported attachments")
            if curr_node.fetch_next_failed or (curr_node.next_msg != None and len(messages) == max_messages):
                user_warnings.add(f"⚠️ Only using last {len(messages)} message{'' if len(messages) == 1 else 's'}")

            curr_msg = curr_node.next_msg

    logging.info(f"Message received (user ID: {new_msg.author.id}, attachments: {len(new_msg.attachments)}, conversation length: {len(messages)}):\n{new_msg.content}")

    system_prompt,location_context = (await resolve_config_location(new_msg,cfg.get('system_prompts',{})))
    if system_prompt:
        system_prompt = [system_prompt]
    else:
        system_prompt = []
    sps,user_context,role_context = resolve_config_user(new_msg,cfg.get('system_prompts',{}))
    system_prompt.extend(sps)
    system_prompt = '\n'.join(system_prompt).strip()

    if system_prompt:
        system_prompt_extras = [f"Current time: {dt.now().strftime('%B %d, %Y %I:%M:%S %p %Z')}."]
        if accept_usernames:
            system_prompt_extras.append("User's names are their Discord IDs and should be typed as '<@ID>'.")
        else:
            system_prompt_extras.append(f"Your Discord display name {discord_client.user.display_name} and your Discord mention is @{discord_client.user.mention}. If you see this, you are being directly addressed. Messages from users are automatically pre-pended with a [From:<user_mention>] behind the scenes to provide you with additional context.")
            #logging.info(system_prompt_extras)

        full_system_prompt = dict(role="system", content="\n".join([system_prompt] + system_prompt_extras))
        messages.append(full_system_prompt)

    # Generate and send response message(s) (can be multiple if response is long)
    response_msgs = []
    response_contents = []
    prev_chunk = None
    edit_task = None
    new_codeblock = False

    kwargs = dict(model=model, messages=messages[::-1], stream=True, extra_body=cfg["extra_api_parameters"])
    try:
        async with new_msg.channel.typing():
            async for curr_chunk in await openai_client.chat.completions.create(**kwargs):
                prev_content = prev_chunk.choices[0].delta.content if prev_chunk != None and prev_chunk.choices[0].delta.content else ""
                curr_content = curr_chunk.choices[0].delta.content or ""

                if response_contents or prev_content:
                    codeblock_end_buffer = len('```\n')
                    effective_max_message_length = max_message_length - codeblock_end_buffer
                    

                    
                    if response_contents == [] or len(rcp := response_contents[-1] + prev_content) > effective_max_message_length:
                        if len(response_contents) > 0 and len(rcp) > effective_max_message_length and new_codeblock:
                            response_contents.append('```\n')
                        else:
                            response_contents.append('')
                    
                        if not use_plain_responses:
                            embed = discord.Embed(description=(prev_content + STREAMING_INDICATOR), color=EMBED_COLOR_INCOMPLETE)
                            footers = ['─'*60,f'Model: {model}']
                            contexts = []
                            if location_context is not None:
                                contexts.append(location_context)
                            if user_context:
                                contexts.append('User')
                            if role_context:
                                contexts.append('Role')
                            if len(contexts) > 0:
                                footers.append('Context: ' + ' • '.join(contexts))
                            embed.set_footer(text='\n'.join(footers))#, style=discord.FooterStyle(color=discord.Color.light_grey()))
                            for warning in sorted(user_warnings):
                                embed.add_field(name=warning, value="", inline=False)

                            reply_to_msg = new_msg if response_msgs == [] else response_msgs[-1]
                            response_msg = await reply_to_msg.reply(embed=embed, silent=True)
                            msg_nodes[response_msg.id] = MsgNode(next_msg=new_msg)
                            await msg_nodes[response_msg.id].lock.acquire()
                            response_msgs.append(response_msg)
                            last_task_time = dt.now().timestamp()

                    response_contents[-1] += prev_content

                    if not use_plain_responses:
                        finish_reason = curr_chunk.choices[0].finish_reason

                        ready_to_edit: bool = (edit_task == None or edit_task.done()) and dt.now().timestamp() - last_task_time >= EDIT_DELAY_SECONDS
                        msg_split_incoming: bool = len(response_contents[-1] + curr_content) > effective_max_message_length
                        is_final_edit: bool = finish_reason != None or msg_split_incoming
                        is_good_finish: bool = finish_reason != None and any(finish_reason.lower() == x for x in ("stop", "end_turn"))

                        new_codeblock = False
                        if msg_split_incoming:
                            if (response_contents[-1].count('```') % 2) == 1:
                                response_contents[-1] += '```\n'
                                new_codeblock = True


                        if ready_to_edit or is_final_edit:
                            if edit_task != None:
                                await edit_task

                            embed.description = response_contents[-1] if is_final_edit else (response_contents[-1] + STREAMING_INDICATOR)
                            embed.color = EMBED_COLOR_COMPLETE if msg_split_incoming or is_good_finish else EMBED_COLOR_INCOMPLETE
                            edit_task = asyncio.create_task(response_msgs[-1].edit(embed=embed))
                            last_task_time = dt.now().timestamp()

                prev_chunk = curr_chunk

        if use_plain_responses:
            for content in response_contents:
                reply_to_msg = new_msg if response_msgs == [] else response_msgs[-1]
                response_msg = await reply_to_msg.reply(content=content, suppress_embeds=True)
                msg_nodes[response_msg.id] = MsgNode(next_msg=new_msg)
                await msg_nodes[response_msg.id].lock.acquire()
                response_msgs.append(response_msg)
    except:
        logging.exception("Error while generating response")

    for response_msg in response_msgs:
        msg_nodes[response_msg.id].text = "".join(response_contents)
        msg_nodes[response_msg.id].lock.release()

    # Delete oldest MsgNodes (lowest message IDs) from the cache
    if (num_nodes := len(msg_nodes)) > MAX_MESSAGE_NODES:
        remove_count= num_nodes - MAX_MESSAGE_NODES
        to_remove = list(msg_nodes.keys())[:remove_count]
        
        for msg_id in to_remove:
            node = msg_nodes.get(msg_id)
            if node:
                async with node.lock:
                    msg_nodes.pop(msg_id, None)



async def main():
    await discord_client.start(cfg["bot_token"])

async def resolve_config_location(new_msg,config_node):
    # if the channel has a channel-specific prompt, then use that, otherwise use the category-specific prompt if it exists, otherwise use the default system prompt
    # note: If the user creates a thread in the channel, then new_msg.channel.parent_id will reflect the overall channel ID 
    channel = new_msg.channel
    if isinstance(channel,discord.Thread):
        threads = channel.parent.threads
    else:
        if hasattr(channel,'threads'):
            threads = channel.threads
        else:
            threads = None

    if threads is not None and (system_prompt_thread := discord.utils.get(threads,name='system-prompt')):
        async for message in system_prompt_thread.history(limit=1):

            if content := message.content.strip():
                total_content = [content]
            else:
                total_content = []

            for att in message.attachments:
                try:
                    content = (await att.read()).decode('utf-8',errors='replace')
                    total_content.append(content.strip())
                except Exception as e:
                    logging.error(f'Failed to read attachment {att.filename}: {e}')
            return ('\n'.join(total_content),'Prompt Thread')

    channel_id = channel.id
    parent_channel_id = getattr(new_msg.channel,'parent_id',None)
    category_id = getattr(new_msg.channel, 'category_id', None)

    if isinstance(channel,discord.Thread):
        topic = channel.parent.topic
    else:
        if hasattr(channel,'topic'):
            topic = channel.topic
        else:
            topic = None

    if topic:
        if result := PROMPT_PATTERN.search(topic):
            return (result.group(1).strip(),'Channel Topic')

    if result := config_node.get(channel_id) or config_node.get(parent_channel_id):
        return (result,'Channel Config')
    if result := config_node.get(category_id):
        return (result,'Category Config')
    if result := config_node.get('default'):
        return (result,'Default')
    return (None,None)

def resolve_config_user(new_msg,config_node):
    # if the channel has a channel-specific prompt, then use that, otherwise use the category-specific prompt if it exists, otherwise use the default system prompt
    # note: If the user creates a thread in the channel, then new_msg.channel.parent_id will reflect the overall channel ID 
    user_id = new_msg.author.id
    if hasattr(new_msg.author,'roles'):
        role_ids = [role.id for role in new_msg.author.roles]
    else:
        role_ids = []
    result = []
    user_context = False
    role_context = False
    if user_result := config_node.get(user_id):
        #logging.info(f'User config: {user_id}')
        result.append(user_result)
        user_context = True
    for role_id in role_ids:
        if role_result := config_node.get(role_id):
            #logging.info(f'Role config: {role_id}')
            result.append(role_result)
            role_context = True
    if len(result) == 0:
        result.append(config_node.get('default'))
    return result, user_context, role_context

asyncio.run(main())

