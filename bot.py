# -*- coding: utf-8 -*-
"""
Render（Background Worker）部署版
- python-telegram-bot v20.x
- 使用 run_polling()，无需 webhook / 域名 / 证书
- 功能：
  1) 用户私聊 -> 群里对应论坛话题（自动创建/复用）：
     - 文本/单媒体直接转
     - 相册/混合媒体聚合后一次性 send_media_group，把第一条非空 caption 提升到首项
  2) 群里管理员在对应话题里“回复机器人发的那条消息” -> 回发给用户（文本/单媒体/相册都支持）
- 注意：
  - sendMediaGroup 单次最多 10 个，已自动分批
  - Telegram 只保留相册第一条 caption，其余会被忽略（API 限制）
- 环境变量：
  - BOT_TOKEN        必填（你的机器人 token）
  - GROUP_CHAT_ID    必填（论坛群 ID，如 -1002914993285）
  - USER_TOPICS_FILE 可选（默认 user_topics.json；Render 建议设为 /var/data/user_topics.json）
"""

import os
import json
import logging
import asyncio
from collections import defaultdict
from typing import List, Dict, Any, Optional

from telegram import (
    Update,
    InputMediaPhoto,
    InputMediaVideo,
    InputMediaDocument,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.error import TelegramError

# ========= 配置 =========
BOT_TOKEN = os.getenv("BOT_TOKEN")
GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID", "0"))
USER_TOPICS_FILE = os.getenv("USER_TOPICS_FILE", "user_topics.json")

if not BOT_TOKEN or GROUP_CHAT_ID == 0:
    raise RuntimeError("请设置 BOT_TOKEN 与 GROUP_CHAT_ID 环境变量")

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
logger = logging.getLogger("bot")

# ========= 数据持久化（用户 <-> 话题）=========
user_topics: Dict[int, Dict[str, Any]] = {}
topic_to_user: Dict[int, int] = {}

def load_maps():
    global user_topics, topic_to_user
    if os.path.exists(USER_TOPICS_FILE):
        try:
            with open(USER_TOPICS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            user_topics = {int(k): v for k, v in data.get("user_topics", {}).items()}
            topic_to_user = {int(k): int(v) for k, v in data.get("topic_to_user", {}).items()}
            logger.info("映射加载成功：%d users, %d topics", len(user_topics), len(topic_to_user))
        except Exception as e:
            logger.error("加载映射失败: %s", e)
            user_topics, topic_to_user = {}, {}
    else:
        user_topics, topic_to_user = {}, {}

def save_maps():
    try:
        data = {"user_topics": user_topics, "topic_to_user": topic_to_user}
        with open(USER_TOPICS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error("保存映射失败: %s", e)

# ========= 相册聚合 =========
media_groups: Dict[str, List[Any]] = defaultdict(list)
media_group_tasks: Dict[str, asyncio.Task] = {}

def _chunk(lst: List[Any], size: int) -> List[List[Any]]:
    return [lst[i:i+size] for i in range(0, len(lst), size)]

def _username_from_update(update: Update) -> str:
    u = update.effective_user
    if not u:
        return "访客"
    username = u.username or f"{u.first_name or ''} {u.last_name or ''}".strip()
    return username or "访客"

def _first_non_empty_caption(msgs: List[Any]) -> str:
    for m in msgs:
        cap = getattr(m, "caption", None)
        if cap:
            return cap
    return ""

async def process_media_group_UG(context: ContextTypes.DEFAULT_TYPE, key: str, topic_id: Optional[int], username: str):
    """用户 -> 群 相册聚合"""
    await asyncio.sleep(1.0)
    msgs = media_groups.pop(key, [])
    media_group_tasks.pop(key, None)
    if not msgs:
        return

    first_caption = _first_non_empty_caption(msgs)
    media_all = []
    for i, m in enumerate(msgs):
        cap = (first_caption if i == 0 else None) or ""
        if m.photo:
            media_all.append(InputMediaPhoto(m.photo[-1].file_id, caption=cap))
        elif m.video:
            media_all.append(InputMediaVideo(m.video.file_id, caption=cap))
        elif m.document:
            media_all.append(InputMediaDocument(m.document.file_id, caption=cap))

    for part in _chunk(media_all, 10):
        kwargs = {"chat_id": GROUP_CHAT_ID, "media": part}
        if topic_id is not None:
            kwargs["message_thread_id"] = topic_id
        await context.bot.send_media_group(**kwargs)

async def process_media_group_GU(context: ContextTypes.DEFAULT_TYPE, key: str, user_id: int):
    """群 -> 用户 相册聚合"""
    await asyncio.sleep(1.0)
    msgs = media_groups.pop(key, [])
    media_group_tasks.pop(key, None)
    if not msgs:
        return

    first_caption = _first_non_empty_caption(msgs)
    media_all = []
    for i, m in enumerate(msgs):
        cap = (first_caption if i == 0 else None) or ""
        if m.photo:
            media_all.append(InputMediaPhoto(m.photo[-1].file_id, caption=cap))
        elif m.video:
            media_all.append(InputMediaVideo(m.video.file_id, caption=cap))
        elif m.document:
            media_all.append(InputMediaDocument(m.document.file_id, caption=cap))

    for part in _chunk(media_all, 10):
        await context.bot.send_media_group(chat_id=user_id, media=part)

# ========= 业务逻辑 =========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("您好！我是客服机器人，请发送消息与我联系。")

async def get_or_create_topic(context: ContextTypes.DEFAULT_TYPE, user_id: int, username: str) -> Optional[int]:
    if user_id in user_topics:
        user_topics[user_id]["username"] = username
        save_maps()
        return user_topics[user_id]["topic_id"]

    try:
        topic = await context.bot.create_forum_topic(chat_id=GROUP_CHAT_ID, name=username or "访客")
        topic_id = topic.message_thread_id
        user_topics[user_id] = {"topic_id": topic_id, "username": username}
        topic_to_user[topic_id] = user_id
        save_maps()
        return topic_id
    except TelegramError as e:
        logger.error("创建论坛话题失败，降级为直发到群: %s", e)
        user_topics[user_id] = {"topic_id": None, "username": username}
        save_maps()
        return None

async def handle_private(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return
    user = msg.from_user
    username = _username_from_update(update)
    topic_id = await get_or_create_topic(context, user.id, username)

    if msg.media_group_id:
        key = f"UG:{msg.media_group_id}"
        media_groups[key].append(msg)
        if key not in media_group_tasks:
            media_group_tasks[key] = context.application.create_task(
                process_media_group_UG(context, key, topic_id, username)
            )
        return

    if msg.text:
        text = f"收到 {username} 的信息：\n{msg.text}"
        kwargs = {"chat_id": GROUP_CHAT_ID, "text": text}
        if topic_id is not None:
            kwargs["message_thread_id"] = topic_id
        await context.bot.send_message(**kwargs)
        return

    cap = msg.caption or ""
    if msg.photo:
        kwargs = {"chat_id": GROUP_CHAT_ID, "photo": msg.photo[-1].file_id,
                  "caption": f"收到 {username} 的照片\n{cap}" if cap else f"收到 {username} 的照片"}
        if topic_id is not None:
            kwargs["message_thread_id"] = topic_id
        await context.bot.send_photo(**kwargs); return

    if msg.video:
        kwargs = {"chat_id": GROUP_CHAT_ID, "video": msg.video.file_id,
                  "caption": f"收到 {username} 的视频\n{cap}" if cap else f"收到 {username} 的视频"}
        if topic_id is not None:
            kwargs["message_thread_id"] = topic_id
        await context.bot.send_video(**kwargs); return

    if msg.document:
        kwargs = {"chat_id": GROUP_CHAT_ID, "document": msg.document.file_id,
                  "caption": f"收到 {username} 的文件\n{cap}" if cap else f"收到 {username} 的文件"}
        if topic_id is not None:
            kwargs["message_thread_id"] = topic_id
        await context.bot.send_document(**kwargs); return

    if msg.voice:
        kwargs = {"chat_id": GROUP_CHAT_ID, "voice": msg.voice.file_id,
                  "caption": f"收到 {username} 的语音\n{cap}" if cap else f"收到 {username} 的语音"}
        if topic_id is not None:
            kwargs["message_thread_id"] = topic_id
        await context.bot.send_voice(**kwargs); return

async def handle_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return
    if msg.chat_id != GROUP_CHAT_ID:
        return
    if not (msg.message_thread_id and msg.reply_to_message and msg.reply_to_message.from_user and msg.reply_to_message.from_user.id == context.bot.id):
        return

    topic_id = msg.message_thread_id
    user_id = topic_to_user.get(topic_id)
    if not user_id:
        for uid, data in user_topics.items():
            if data.get("topic_id") == topic_id:
                user_id = uid
                topic_to_user[topic_id] = uid
                save_maps()
                break
    if not user_id:
        logger.warning("未找到 topic_id %s 对应用户", topic_id)
        return

    if msg.media_group_id:
        key = f"GU:{msg.media_group_id}"
        media_groups[key].append(msg)
        if key not in media_group_tasks:
            media_group_tasks[key] = context.application.create_task(
                process_media_group_GU(context, key, user_id)
            )
        return

    if msg.text:
        await context.bot.send_message(chat_id=user_id, text=msg.text); return

    cap = msg.caption or ""
    if msg.photo:
        await context.bot.send_photo(chat_id=user_id, photo=msg.photo[-1].file_id, caption=cap); return
    if msg.video:
        await context.bot.send_video(chat_id=user_id, video=msg.video.file_id, caption=cap); return
    if msg.document:
        await context.bot.send_document(chat_id=user_id, document=msg.document.file_id, caption=cap); return
    if msg.voice:
        await context.bot.send_voice(chat_id=user_id, voice=msg.voice.file_id, caption=cap); return

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("更新 %s 出错: %s", update, context.error)

def main():
    load_maps()
    app: Application = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, handle_private))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & ~filters.COMMAND, handle_group))
    app.add_error_handler(error_handler)
    logger.info("Bot started. Forwarding & media-group aggregation is active.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
