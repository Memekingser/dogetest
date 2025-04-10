# -*- coding: utf-8 -*-

import os
import time
import json
import asyncio
import aiohttp
from datetime import datetime, timezone
from dotenv import load_dotenv
from telegram import Bot
from telegram.constants import ParseMode
import logging
import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import schedule

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)

# 加载环境变量
load_dotenv()

# Telegram配置
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
COINGECKO_API_URL = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,dogecoin&vs_currencies=usd"
FALLBACK_BTC_PRICE = 90000  # 如果API获取失败时的备用价格
FALLBACK_DOGE_PRICE = 0.15  # 如果API获取失败时的备用DOGE价格
BTC_PRICE_UPDATE_INTERVAL = 60  # 每60秒更新一次价格

# 检查Telegram配置
if not TELEGRAM_TOKEN:
    print("错误: 请在.env文件中设置TELEGRAM_TOKEN")
    print("示例:")
    print("TELEGRAM_TOKEN=你的机器人Token")
    exit(1)

# Odin配置
TOKEN_ID = "229u"
API_URL = "https://api.dogepump.ai/v4/swap_v2/order"

# 全局变量
current_btc_price = FALLBACK_BTC_PRICE
current_doge_price = FALLBACK_DOGE_PRICE
last_btc_price_update = 0

# 用于存储已处理的订单和最后检查时间
processed_orders = set()
last_check_time = None
active_chats = set()  # 存储活跃的群组ID
last_update_id = 0  # 存储最后处理的更新ID

def load_active_chats():
    """从文件加载活跃的群组ID"""
    try:
        with open('active_chats.json', 'r') as f:
            chats = json.load(f)
            active_chats.update(chats)
            log_message(f"已加载 {len(active_chats)} 个活跃群组")
    except FileNotFoundError:
        log_message("未找到活跃群组记录文件，将创建新文件")
        save_active_chats()

def save_active_chats():
    """保存活跃的群组ID到文件"""
    with open('active_chats.json', 'w') as f:
        json.dump(list(active_chats), f)
    log_message(f"已保存 {len(active_chats)} 个活跃群组")

def log_message(message):
    """输出带时间戳的日志"""
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("[{}] {}".format(current_time, message))

async def handle_command(bot: Bot, chat_id: int, command: str, chat_title: str = "未知群组"):
    """处理命令"""
    if command == "/start":
        if chat_id not in active_chats:
            active_chats.add(chat_id)
            save_active_chats()
            await bot.send_message(
                chat_id=chat_id,
                text="✅ 机器人已成功启动！\n将在此发送新的买单提醒。\n如需停止接收提醒，请发送 /stop",
                parse_mode=ParseMode.HTML
            )
            log_message(f"新群组已激活: {chat_title} ({chat_id})")
        else:
            await bot.send_message(
                chat_id=chat_id,
                text="机器人已经在运行中！\n如需停止接收提醒，请发送 /stop",
                parse_mode=ParseMode.HTML
            )
    elif command == "/stop":
        if chat_id in active_chats:
            active_chats.remove(chat_id)
            save_active_chats()
            await bot.send_message(
                chat_id=chat_id,
                text="❌ 已停止发送买单提醒！\n如需重新开启，请发送 /start",
                parse_mode=ParseMode.HTML
            )
            log_message(f"群组已停用: {chat_title} ({chat_id})")
        else:
            await bot.send_message(
                chat_id=chat_id,
                text="机器人尚未启动！\n如需开启提醒，请发送 /start",
                parse_mode=ParseMode.HTML
            )

async def check_commands():
    """检查新的命令"""
    global last_update_id
    bot = Bot(token=TELEGRAM_TOKEN)
    
    try:
        updates = await bot.get_updates(offset=last_update_id + 1, timeout=1)
        for update in updates:
            if update.message and update.message.text:
                command = update.message.text.lower()
                chat_id = update.message.chat_id
                chat_title = update.message.chat.title or "私聊"
                
                if command in ["/start", "/stop"]:
                    await handle_command(bot, chat_id, command, chat_title)
            
            last_update_id = update.update_id
    except Exception as e:
        log_message(f"检查命令时出错: {e}")

async def send_telegram_message(message):
    """发送消息到所有活跃的Telegram群组"""
    bot = Bot(token=TELEGRAM_TOKEN)
    
    for chat_id in active_chats.copy():
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=message,
                parse_mode=ParseMode.HTML
            )
            log_message(f"成功发送消息到群组 {chat_id}")
        except Exception as e:
            log_message(f"发送消息到群组 {chat_id} 失败: {e}")
            if "Chat not found" in str(e) or "Forbidden" in str(e):
                log_message(f"群组 {chat_id} 不可访问，已移除")
                active_chats.remove(chat_id)
                save_active_chats()

async def fetch_trades():
    """获取最新交易数据"""
    global last_check_time
    
    if last_check_time is None:
        last_check_time = get_current_time_iso()
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'Accept': 'application/json',
        'Accept-Language': 'en-US,en;q=0.9',
        'Content-Type': 'application/json',
        'Origin': 'https://dogepump.ai',
        'Referer': 'https://dogepump.ai/',
        'Connection': 'keep-alive',
        'Sec-Fetch-Dest': 'empty',
        'Sec-Fetch-Mode': 'cors',
        'Sec-Fetch-Site': 'same-site'
    }
    
    # 添加请求体
    payload = {
        "page": 1,
        "limit": 100,
        "time_min": last_check_time
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(API_URL, headers=headers, json=payload) as response:
                if response.status == 200:
                    try:
                        response_text = await response.text()
                        if not response_text:
                            log_message("API返回空响应")
                            return None
                            
                        data = json.loads(response_text)
                        if data.get('code') == 200 and data.get('msg') == 'success':
                            log_message("成功获取数据")
                            # 更新最后检查时间
                            last_check_time = get_current_time_iso()
                            return data
                        else:
                            log_message(f"API返回错误: {data.get('msg', '未知错误')}")
                            return None
                    except json.JSONDecodeError as e:
                        log_message(f"JSON解析失败: {e}")
                        log_message(f"响应内容: {response_text[:500]}")  # 只显示前500个字符
                        return None
                    except Exception as e:
                        log_message(f"处理响应时出错: {str(e)}")
                        return None
                else:
                    log_message(f"获取数据失败，状态码: {response.status}")
                    return None
    except aiohttp.ClientError as e:
        log_message(f"请求出错: {str(e)}")
        return None
    except Exception as e:
        log_message(f"未知错误: {str(e)}")
        return None

def update_btc_price():
    """从CoinGecko获取实时BTC和DOGE价格"""
    global current_btc_price, current_doge_price, last_btc_price_update
    
    try:
        current_time = time.time()
        if current_time - last_btc_price_update < BTC_PRICE_UPDATE_INTERVAL:
            return current_btc_price, current_doge_price
            
        response = requests.get(COINGECKO_API_URL)
        if response.status_code == 200:
            data = response.json()
            current_btc_price = data['bitcoin']['usd']
            current_doge_price = data['dogecoin']['usd']
            last_btc_price_update = current_time
            logging.info(f"价格更新成功: BTC=${current_btc_price:,.2f}, DOGE=${current_doge_price:.4f}")
            return current_btc_price, current_doge_price
        else:
            logging.error(f"获取价格失败: HTTP {response.status_code}")
            return FALLBACK_BTC_PRICE, FALLBACK_DOGE_PRICE
    except Exception as e:
        logging.error(f"获取价格时发生错误: {str(e)}")
        return FALLBACK_BTC_PRICE, FALLBACK_DOGE_PRICE

async def process_trades(trades_data):
    """处理交易数据"""
    if not trades_data or not isinstance(trades_data, dict):
        return
    
    try:
        # 更新BTC和DOGE价格
        current_btc_price, current_doge_price = update_btc_price()
        
        orders = trades_data.get('data', [])
        if not isinstance(orders, list):
            log_message("无效的订单数据格式")
            return
            
        log_message("获取到 {} 笔订单".format(len(orders)))
        
        for order in orders:
            # 检查是否为swap操作且tick1为test
            if (isinstance(order, dict) and 
                order.get('op') == 'swap' and 
                order.get('tick1') == 'test'):
                
                order_key = "{}_{}_{}_{}".format(
                    order.get('id', ''),
                    order.get('order_id', ''),
                    order.get('amt0', ''),
                    order.get('amt1_out', '')
                )
                
                if order_key not in processed_orders:
                    processed_orders.add(order_key)
                    log_message("发现新交易订单:")
                    for key, value in order.items():
                        log_message("- {}: {}".format(key, value))
                    
                    # 获取原始数据
                    amt0 = float(order.get('amt0', '0'))  # 输入金额
                    amt1_out = float(order.get('amt1_out', '0'))  # 输出金额
                    
                    # 计算DOGE数量和美元价值
                    doge_amount = amt0 / 1e8
                    doge_value = doge_amount * current_doge_price
                    
                    # 只处理1美金及以上的订单
                    if doge_value < 1:
                        continue
                    
                    message = (
                        "New $TEST Buy Order!!!!!!\n"
                        "🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢\n"
                        "🔄 BUY! BUY! BUY!\n"
                        "💵 Buy: {amt0:.2f} $DOGE (${doge_value:.2f})\n"
                        "📊 Get: {amt1_out:.2f} $TEST\n"
                        "🔗 Trding Hash: {tx_hash}\n"
                        "👤 Address: {holder_address}"
                    ).format(
                        amt0=doge_amount,
                        doge_value=doge_value,
                        amt1_out=amt1_out / 1e8,
                        tx_hash=order.get('tx_hash', '未知'),
                        holder_address=order.get('holder_address', '未知地址')
                    )
                    await send_telegram_message(message)
    
    except Exception as e:
        log_message("处理交易数据时出错: {}".format(e))

def format_amount(amount):
    """格式化数量，添加千位分隔符"""
    return "{:,}".format(amount)

def format_k_sats(sats):
    """将sats格式化为k单位"""
    k_value = sats / 1000000000
    return f"{k_value:,.1f}k"

def format_token_amount(amount):
    """将token数量格式化为两位小数"""
    token_amount = amount / 100000000000
    return f"{token_amount:.2f}"  # 除以10^8并保留两位小数

def format_sats_price(sats):
    """格式化sats价格，保留2位小数"""
    return f"{sats / 1000:.2f}"  # 除以1000显示为k并保留2位小数

def format_price_usd(sats_price):
    """计算并格式化USD价格"""
    usd_price = (sats_price / 100000000000000) * current_btc_price
    return f"{usd_price:.2f}"

def format_time(time_str):
    """格式化时间为本地时间"""
    try:
        dt = datetime.strptime(time_str, "%Y-%m-%dT%H:%M:%S.%fZ")
        local_time = dt.strftime("%Y-%m-%d %H:%M:%S")
        return local_time
    except:
        return time_str

def get_current_time_iso():
    """获取当前UTC时间的ISO格式"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

async def main():
    log_message("机器人启动中...")
    
    # 加载已保存的活跃群组
    load_active_chats()
    
    log_message("机器人启动 - 每2秒检查一次新交易")
    
    while True:
        try:
            # 检查新命令
            await check_commands()
            
            # 检查新交易
            trades_data = await fetch_trades()
            await process_trades(trades_data)
        except Exception as e:
            log_message("运行出错: {}".format(e))
        await asyncio.sleep(1)

if __name__ == "__main__":
    asyncio.run(main()) 
