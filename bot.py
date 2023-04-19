import datetime
import json
import os
import re
import urllib.parse

import telegram
from telegram import (BotCommand, InlineKeyboardButton, InlineKeyboardMarkup,
                      Update)
from telegram.constants import ParseMode
from telegram.ext import (Application, ApplicationBuilder,
                          CallbackQueryHandler, CommandHandler, MessageHandler)

from config import config
from utils.bard_utils import Bard
from utils.claude_utils import Claude


script_path = os.path.dirname(os.path.realpath(__file__))
os.chdir(script_path)

token = config.telegram_token
admin_id = config.telegram_username
fine_granted_identifier = []

# load from fine_granted_identifier.json if exists
try:
    with open('fine_granted_identifier.json', 'r') as f:
        fine_granted_identifier = json.load(f)
except Exception as e:
    pass


chat_context_container = {}


def create_session(mode='claude', id=None):
    mode_to_class = {'claude': Claude, 'bard': Bard}
    cls = mode_to_class.get(mode.lower(), Claude)
    return cls(id)


def validate_user(update: Update) -> bool:
    identifier = user_identifier(update)
    return identifier in admin_id or identifier in fine_granted_identifier


def check_timestamp(update: Update) -> bool:
    # check timestamp
    global boot_time
    # if is earlier than boot time, ignore
    message_utc_timestamp = update.message.date.timestamp()
    boot_utc_timestamp = boot_time.timestamp()
    return message_utc_timestamp >= boot_utc_timestamp


def check_should_handle(update: Update, context) -> bool:
    if not check_timestamp(update):
        return False

    if update.message is None or update.message.text is None or len(update.message.text) == 0:
        return False

    # if is a private chat
    if update.effective_chat.type == 'private':
        return True

    # if replying to ourself
    if (
        True
        and (update.message.reply_to_message is not None)
        and (update.message.reply_to_message.from_user is not None)
        and (update.message.reply_to_message.from_user.id is not None)
        and (update.message.reply_to_message.from_user.id == context.bot.id)
    ):
        return True

    # if mentioning ourself, at the beginning of the message
    if update.message.entities is not None:
        for entity in update.message.entities:
            if (
                True
                and (entity.type is not None)
                and (entity.type == 'mention')
                # and (entity.user is not None)
                # and (entity.user.id is not None)
                # and (entity.user.id == context.bot.id)
            ):
                mention_text = update.message.text[entity.offset:entity.offset + entity.length]
                if not mention_text == f'@{context.bot.username}':
                    continue
                return True

    return False


def user_identifier(update: Update) -> str:
    return f'{update.effective_chat.id}'


async def reset_chat(update: Update, context):
    if not check_timestamp(update):
        return
    if not validate_user(update):
        await update.message.reply_text('❌ Sadly, you are not allowed to use this bot at this time.')
        return

    user_id = user_identifier(update)
    if user_id in chat_context_container:
        chat_context_container[user_id].reset()
        await update.message.reply_text('✅ Chat history has been reset.')
    else:
        await update.message.reply_text('❌ Chat history is empty.')


# Google bard: view other drafts
async def view_other_drafts(update: Update, context):
    if update.callback_query.data == 'drafts':
        # increase choice index
        context.user_data['param']['index'] = (
            context.user_data['param']['index'] + 1) % len(context.user_data['param']['choices'])
        await bard_response(**context.user_data['param'])


# Google bard: response
async def bard_response(client, message, markup, sources, choices, index):
    client.choice_id = choices[index]['id']
    content = choices[index]['content'][0]
    _content = re.sub(
        r'[\_\*\[\]\(\)\~\>\#\+\-\=\|\{\}\.\!]', lambda x: f'\\{x.group(0)}', content).replace('\\*\\*', '*')
    _sources = re.sub(
        r'[\_\*\[\]\(\)\~\`\>\#\+\-\=\|\{\}\.\!]', lambda x: f'\\{x.group(0)}', sources)
    try:
        await message.edit_text(f'{_content}{_sources}', reply_markup=markup, parse_mode=ParseMode.MARKDOWN_V2)
    except telegram.error.BadRequest as e:
        if str(e).startswith('Message is not modified'):
            await message.edit_text(f'{_content}{_sources}\n\\.', reply_markup=markup, parse_mode=ParseMode.MARKDOWN_V2)
        else:
            await message.edit_text(f'{content}{sources}\n\n❌ Markdown failed.', reply_markup=markup)


# reply. Stream chat for claude
async def recv_msg(update: Update, context):
    if not check_should_handle(update, context):
        return
    if not validate_user(update):
        await update.message.reply_text('❌ Sadly, you are not allowed to use this bot at this time.')
        return

    chat_session = chat_context_container.get(user_identifier(update))
    if chat_session is None:
        chat_session = create_session(id=user_identifier(update))
        chat_context_container[user_identifier(update)] = chat_session

    message = await update.message.reply_text(
        '... thinking ...'
    )
    if message is None:
        return

    try:
        input_text = update.message.text
        # remove bot name from text with @
        pattern = f'@{context.bot.username}'
        input_text = input_text.replace(pattern, '')
        current_mode = chat_session.get_mode()

        if current_mode == 'bard':
            response = chat_session.send_message(input_text)
            # get source links
            sources = ''
            if response['factualityQueries']:
                links = set(
                    item[2][0] for item in response['factualityQueries'][0] if item[2][0] != '')
                sources = '\n\nSources - Learn More\n' + \
                    '\n'.join([f'{i+1}. {val}' for i, val in enumerate(links)])

            # Buttons
            search_url = f"https://www.google.com/search?q={urllib.parse.quote(response['textQuery'][0]) if response['textQuery'] != '' else urllib.parse.quote(input_text)}"
            markup = InlineKeyboardMarkup([[InlineKeyboardButton(text='📝 View other drafts', callback_data='drafts'),
                                            InlineKeyboardButton(text='🔍 Google it', url=search_url)]])
            context.user_data['param'] = {'client': chat_session.client, 'message': message,
                                          'markup': markup, 'sources': sources, 'choices': response['choices'], 'index': 0}
            # get response
            await bard_response(**context.user_data['param'])

        else:  # Claude
            prev_response = ''
            for response in chat_session.send_message_stream(input_text):
                if abs(len(response) - len(prev_response)) < 100:
                    continue
                prev_response = response
                await message.edit_text(response)

            _response = re.sub(
                r'[\_\*\[\]\(\)\~\>\#\+\-\=\|\{\}\.\!]', lambda x: f'\\{x.group(0)}', response)
            try:
                await message.edit_text(_response, parse_mode=ParseMode.MARKDOWN_V2)
            except telegram.error.BadRequest as e:
                if str(e).startswith('Message is not modified'):
                    await message.edit_text(f'{_response}\n\\.', parse_mode=ParseMode.MARKDOWN_V2)
                else:
                    await message.edit_text(f'{response}\n\n❌ Markdown failed.')

    except Exception as e:
        chat_session.reset()
        await message.edit_text('❌ Error orrurred, please try again later. Your chat history has been reset.')


# Settings
async def show_settings(update: Update, context):
    if not check_timestamp(update):
        return
    if not validate_user(update):
        await update.message.reply_text('❌ Sadly, you are not allowed to use this bot at this time.')
        return

    chat_session = chat_context_container.get(user_identifier(update))
    if chat_session is None:
        chat_session = create_session(id=user_identifier(update))
        chat_context_container[user_identifier(update)] = chat_session

    current_mode = chat_session.get_mode()
    infos = [
        f'<b>Current mode:</b> {current_mode}',
    ]
    if current_mode == 'bard':
        extras = [
            '',
            'Commands:',
            '• /mode to use Anthropic Claude',
        ]
    else:  # Claude
        current_model, current_temperature = chat_session.get_settings()
        extras = [
            f'<b>Current model:</b> {current_model}',
            f'<b>Current temperature:</b> {current_temperature}',
            '',
            'Commands:',
            '• /mode to use Google Bard',
            '• [/model NAME] to change model',
            '• [/temp VALUE] to set temperature',
            "<a href='https://console.anthropic.com/docs/api/reference'>Reference</a>",
        ]
    infos.extend(extras)
    await update.message.reply_text('\n'.join(infos), parse_mode=ParseMode.HTML)


async def change_mode(update: Update, context):
    if not check_timestamp(update):
        return
    if not validate_user(update):
        await update.message.reply_text('❌ Sadly, you are not allowed to use this bot at this time.')
        return

    chat_session = chat_context_container.get(user_identifier(update))
    if chat_session is None:
        chat_session = create_session(id=user_identifier(update))
        chat_context_container[user_identifier(update)] = chat_session

    current_mode = chat_session.get_mode()
    final_mode = 'bard' if current_mode == 'claude' else 'claude'
    chat_session = create_session(mode=final_mode, id=user_identifier(update))
    chat_context_container[user_identifier(update)] = chat_session
    await update.message.reply_text(f'✅ Mode has been switched to {final_mode}.')
    await show_settings(update, context)


async def change_model(update: Update, context):
    if not check_timestamp(update):
        return
    if not validate_user(update):
        await update.message.reply_text('❌ Sadly, you are not allowed to use this bot at this time.')
        return

    chat_session = chat_context_container.get(user_identifier(update))
    if chat_session is None:
        chat_session = create_session(id=user_identifier(update))
        chat_context_container[user_identifier(update)] = chat_session

    if chat_session.get_mode() == 'bard':
        await update.message.reply_text('❌ Invalid option for Google Bard.')
        return

    if len(context.args) != 1:
        await update.message.reply_text('❌ Please provide a model name.')
        return
    model = context.args[0].strip()
    if not chat_session.change_model(model):
        await update.message.reply_text('❌ Invalid model name.')
        return
    await update.message.reply_text(f'✅ Model has been switched to {model}.')
    await show_settings(update, context)


async def change_temperature(update: Update, context):
    if not check_timestamp(update):
        return
    if not validate_user(update):
        await update.message.reply_text('❌ Sadly, you are not allowed to use this bot at this time.')
        return

    chat_session = chat_context_container.get(user_identifier(update))
    if chat_session is None:
        chat_session = create_session(id=user_identifier(update))
        chat_context_container[user_identifier(update)] = chat_session

    if chat_session.get_mode() == 'bard':
        await update.message.reply_text('❌ Invalid option for Google Bard.')
        return

    if len(context.args) != 1:
        await update.message.reply_text('❌ Please provide a temperature value.')
        return
    temperature = context.args[0].strip()
    if not chat_session.change_temperature(temperature):
        await update.message.reply_text('❌ Invalid temperature value.')
        return
    await update.message.reply_text(f'✅ Temperature has been set to {temperature}.')
    await show_settings(update, context)


async def start_bot(update: Update, context):
    if not check_timestamp(update):
        return
    id = user_identifier(update)
    welcome_strs = [
        'Welcome to <b>Claude & Bard Telegram Bot</b>',
        '',
        'Commands:',
        '• /id to get your chat identifier',
        '• /reset to reset the chat history',
        '• /mode to switch between Claude & Bard',
        '• /settings to show Claude & Bard settings',
    ]
    if id in admin_id:
        extra = [
            '',
            'Admin Commands:',
            '• /grant to grant fine-granted access to a user',
            '• /ban to ban a user',
            '• /status to report the status of the bot',
            '• /reboot to clear all chat history',
        ]
        welcome_strs.extend(extra)
    print(f'[i] {update.effective_user.username} started the bot')
    await update.message.reply_text('\n'.join(welcome_strs), parse_mode=ParseMode.HTML)


async def send_id(update: Update, context):
    if not check_timestamp(update):
        return
    current_identifier = user_identifier(update)
    await update.message.reply_text(f'Your chat identifier is {current_identifier}, send it to the bot admin to get fine-granted access.')


async def grant(update: Update, context):
    if not check_timestamp(update):
        return
    current_identifier = user_identifier(update)
    if current_identifier not in admin_id:
        await update.message.reply_text('❌ You are not admin!')
        return
    if len(context.args) != 1:
        await update.message.reply_text('❌ Please provide a user id to grant!')
        return
    user_id = context.args[0].strip()
    if user_id in fine_granted_identifier:
        await update.message.reply_text('❌ User already has fine-granted access!')
        return
    fine_granted_identifier.append(user_id)
    with open('fine_granted_identifier.json', 'w') as f:
        json.dump(list(fine_granted_identifier), f)
    await update.message.reply_text('✅ User has been granted fine-granted access!')


async def ban(update: Update, context):
    if not check_timestamp(update):
        return
    current_identifier = user_identifier(update)
    if current_identifier not in admin_id:
        await update.message.reply_text('❌ You are not admin!')
        return
    if len(context.args) != 1:
        await update.message.reply_text('❌ Please provide a user id to ban!')
        return
    user_id = context.args[0].strip()
    if user_id in fine_granted_identifier:
        fine_granted_identifier.remove(user_id)
    if user_id in chat_context_container:
        del chat_context_container[user_id]
    with open('fine_granted_identifier.json', 'w') as f:
        json.dump(list(fine_granted_identifier), f)
    await update.message.reply_text('✅ User has been banned!')


async def status(update: Update, context):
    if not check_timestamp(update):
        return
    current_identifier = user_identifier(update)
    if current_identifier not in admin_id:
        await update.message.reply_text('❌ You are not admin!')
        return
    report = [
        'Status Report:',
        f'[+] bot started at {boot_time}',
        f'[+] admin users: {admin_id}',
        f'[+] fine-granted users: {len(fine_granted_identifier)}',
        f'[+] chat sessions: {len(chat_context_container)}',
        '',
    ]
    # list each fine-granted user
    cnt = 1
    for user_id in fine_granted_identifier:
        report.append(f'[i] {cnt} {user_id}')
        cnt += 1
    await update.message.reply_text(
        '```\n' + '\n'.join(report) + '\n```',
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def reboot(update: Update, context):
    if not check_timestamp(update):
        return
    current_identifier = user_identifier(update)
    if current_identifier not in admin_id:
        await update.message.reply_text('❌ You are not admin!')
        return
    chat_context_container.clear()
    await update.message.reply_text('✅ All chat history has been cleared!')


async def post_init(application: Application):
    await application.bot.set_my_commands([
        BotCommand('/reset', 'Reset the chat history'),
        BotCommand('/mode', 'Switch between Claude & Bard'),
        BotCommand('/settings', 'Show Claude & Bard settings'),
        BotCommand('/help', 'Get help message'),
    ])

boot_time = datetime.datetime.now()

print(f'[+] bot started at {boot_time}, calling loop!')
application = ApplicationBuilder().token(token).post_init(post_init).build()

handler_list = [
    CommandHandler('id', send_id),
    CommandHandler('start', start_bot),
    CommandHandler('help', start_bot),
    CommandHandler('reset', reset_chat),
    CommandHandler('grant', grant),
    CommandHandler('ban', ban),
    CommandHandler('status', status),
    CommandHandler('reboot', reboot),
    CommandHandler('settings', show_settings),
    CommandHandler('mode', change_mode),
    CommandHandler('model', change_model),
    CommandHandler('temp', change_temperature),
    CallbackQueryHandler(view_other_drafts),
    MessageHandler(None, recv_msg),
]
for handler in handler_list:
    application.add_handler(handler)

application.run_polling()
