import asyncio
import threading
import requests
import hmac
import hashlib
import base64
import uuid
import nacl.signing
import time
import random
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
import json
import os
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from decimal import Decimal, ROUND_DOWN

class PersistentHistory(list):
    def __init__(self, filename):
        self.filename = filename
        super().__init__(self.load())

    def append(self, item):
        super().append(item)
        self.save()

    def save(self):
        try:
            with open(self.filename, 'w', encoding='utf-8') as f:
                json.dump(self, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f'[ERROR] Ошибка сохранения истории: {e}')

    def load(self):
        if os.path.exists(self.filename):
            try:
                with open(self.filename, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                print(f'[ERROR] Ошибка загрузки истории: {e}')
        return []

class PersistentConfig:
    def __init__(self, filename):
        self.filename = filename
        self.data = self.load()

    def set(self, key, value):
        self.data[key] = value
        self.save()

    def get(self, key, default=None):
        return self.data.get(key, default)

    def save(self):
        try:
            with open(self.filename, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f'[ERROR] Ошибка сохранения конфига: {e}')

    def load(self):
        if os.path.exists(self.filename):
            try:
                with open(self.filename, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                print(f'[ERROR] Ошибка загрузки конфига: {e}')
        return {}

# === Твои ключи и конфиги ===
TELEGRAM_BOT_TOKEN = ''
ARKHAM_API_KEY = ''
ARKHAM_API_SECRET = ''
ARKHAM_URL = 'https://arkm.com/api'
BACKPACK_PUBLIC_KEY = ''
BACKPACK_PRIVATE_KEY = ''
BACKPACK_URL = 'https://api.backpack.exchange'
ARKHAM_SYMBOL = 'BTC_USDT_PERP'
BACKPACK_SYMBOL = 'BTC_USDC'

# === Глобальные переменные ===
cycle_thread = None
cycle_stop_flag = False

config = PersistentConfig('config.json')
size_position = config.get('size_position', '0.0002')
BACKPACK_SYMBOL = config.get('BACKPACK_SYMBOL', 'BTC_USDC')
ARKHAM_SYMBOL = config.get('ARKHAM_SYMBOL', 'BTC_USDT_PERP')

history = PersistentHistory('history.json')

# === FSM ===
class BotState(StatesGroup):
    idle = State()
    running = State()
    waiting_for_size = State()
    waiting_for_size = State()
    waiting_for_symbol = State()


# === Arkham функции ===
def arkham_signature(api_key, api_secret, method, path, body):
    expires = str((int(time.time()) + 300) * 1000000)
    msg = f'{api_key}{expires}{method}{path}{body}'
    signature = hmac.new(base64.b64decode(api_secret), msg.encode(), hashlib.sha256).digest()
    return base64.b64encode(signature).decode(), expires

def arkham_request(method, path, body=''):
    sig, expires = arkham_signature(ARKHAM_API_KEY, ARKHAM_API_SECRET, method, path, body)
    headers = {
        'Content-Type': 'application/json',
        'Arkham-Api-Key': ARKHAM_API_KEY,
        'Arkham-Expires': expires,
        'Arkham-Signature': sig
    }
    url = f'{ARKHAM_URL}{path}'
    if method == 'POST':
        r = requests.post(url, headers=headers, data=body)
    elif method == 'GET':
        r = requests.get(url, headers=headers)
    return r.json()

def place_arkham_order(symbol, side, qty):
    path = '/orders/new'
    client_order_id = str(uuid.uuid4())
    body = f'''{{
      "clientOrderId": "{client_order_id}",
      "postOnly": false,
      "price": "0",
      "reduceOnly": false,
      "side": "{side.lower()}",
      "size": "{qty}",
      "subaccountId": 0,
      "symbol": "{symbol}",
      "type": "market",
      "marketType": "perp"
    }}'''
    resp = arkham_request('POST', path, body)
    return resp

def get_arkham_position(symbol):
    path = '/account/positions'
    resp = arkham_request('GET', path)
    for p in resp:
        if p['symbol'] == symbol:
            return float(p.get('pnl', 0))
    return None

# === Backpack функции ===
def create_backpack_signature(instruction, params):
    clean_params = {k: str(v) for k, v in params.items()}
    ordered = '&'.join(f'{k}={v}' for k, v in sorted(clean_params.items()))
    sign_str = f'instruction={instruction}&{ordered}'
    private_key = nacl.signing.SigningKey(base64.b64decode(BACKPACK_PRIVATE_KEY))
    signed = private_key.sign(sign_str.encode())
    return base64.b64encode(signed.signature).decode()

def place_backpack_order(symbol, side, qty):
    instruction = 'orderExecute'
    path = '/api/v1/order'
    timestamp = str(int(time.time() * 1000))
    window = '5000'
    side_enum = 'Bid' if side == 'BUY' else 'Ask'
    body = {
        'symbol': symbol,
        'side': side_enum,
        'orderType': 'Market',
        'quantity': qty,
        'timestamp': timestamp,
        'window': window
    }
    signature = create_backpack_signature(instruction, body)
    headers = {
        'Content-Type': 'application/json',
        'X-API-Key': BACKPACK_PUBLIC_KEY,
        'X-Timestamp': timestamp,
        'X-Window': window,
        'X-Signature': signature
    }
    url = f'{BACKPACK_URL}{path}'
    r = requests.post(url, headers=headers, json=body)
    return r.status_code

# === Бизнес-цикл ===
def delta_cycle():
    global cycle_stop_flag, history

    while not cycle_stop_flag:
        # ВСЕГДА достаем из конфига актуальные значения
        size_position = config.get('size_position', '0.0002')
        BACKPACK_SYMBOL = config.get('BACKPACK_SYMBOL', 'BTC_USDC')
        ARKHAM_SYMBOL = config.get('ARKHAM_SYMBOL', 'BTC_USDT_PERP')

        history.append(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] 📈 Открываю Backpack BUY {size_position} {BACKPACK_SYMBOL}')
        place_backpack_order(BACKPACK_SYMBOL, 'BUY', size_position)

        history.append(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] 📉 Открываю Arkham SELL {size_position} {ARKHAM_SYMBOL}')
        place_arkham_order(ARKHAM_SYMBOL, 'sell', size_position)

        hold_duration = random.randint(600, 900)
        start_time = time.time()
        history.append(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] ⏳ Удерживаю позиции {hold_duration // 60} минут')

        while not cycle_stop_flag:
            time.sleep(10)
            if time.time() - start_time > hold_duration:
                history.append(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] ⌛ Время удержания вышло')
                break

        pnl = get_arkham_position(ARKHAM_SYMBOL)
        if pnl is not None:
            history.append(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] 📊 PNL на Arkham перед закрытием: {pnl} USDT')

        history.append(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] 📉 Закрываю Backpack SELL {size_position} {BACKPACK_SYMBOL}')
        place_backpack_order(BACKPACK_SYMBOL, 'SELL', size_position)

        history.append(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] 📈 Закрываю Arkham BUY {size_position} {ARKHAM_SYMBOL} (reduceOnly)')
        close_arkham_position(ARKHAM_SYMBOL, 'buy', size_position)

        history.append(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] ✅ Позиции закрыты, цикл завершен')

        if cycle_stop_flag:
            break

        pause_duration = random.randint(180, 360)
        history.append(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] 💤 Пауза {pause_duration // 60} минут перед следующим циклом')
        time.sleep(pause_duration)

    print('🛑 Закрываю позиции...')
    place_backpack_order(BACKPACK_SYMBOL, 'SELL', size_position)
    close_arkham_position(ARKHAM_SYMBOL, 'buy', size_position)
    print('✅ Позиции закрыты, цикл завершен.')

def close_arkham_position(symbol, side, qty):
    path = '/orders/new'
    client_order_id = str(uuid.uuid4())
    body = f'''{{
      "clientOrderId": "{client_order_id}",
      "postOnly": false,
      "price": "0",
      "reduceOnly": true,
      "side": "{side.lower()}",
      "size": "{qty}",
      "subaccountId": 0,
      "symbol": "{symbol}",
      "type": "market",
      "marketType": "perp"
    }}'''
    resp = arkham_request('POST', path, body)
    return resp

    close_arkham_position(ARKHAM_SYMBOL, 'buy', size_position)
    print('✅ Позиции закрыты')

# === Telegram ===
bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

@dp.message(F.text == '/start')
async def start_cmd(message: Message, state: FSMContext):
    kb = ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text='▶️ Старт цикла')],
        [KeyboardButton(text='⏹️ Стоп цикла')],
        [KeyboardButton(text='📜 История')],
        [KeyboardButton(text='⚙️ Изменить Size')],
        [KeyboardButton(text='🔄 Сменить пару')]
    ], resize_keyboard=True)
    await message.answer('Бот готов, мой господин.', reply_markup=kb)
    await state.set_state(BotState.idle)

@dp.message(F.text == '▶️ Старт цикла')
async def start_cycle(message: Message, state: FSMContext):
    global cycle_thread, cycle_stop_flag
    if cycle_thread and cycle_thread.is_alive():
        await message.answer('Цикл уже запущен.')
    else:
        cycle_stop_flag = False
        cycle_thread = threading.Thread(target=delta_cycle)
        cycle_thread.start()
        await message.answer('🚀 Цикл запущен.')
        await state.set_state(BotState.running)

@dp.message(F.text == '⏹️ Стоп цикла')
async def stop_cycle(message: Message, state: FSMContext):
    global cycle_stop_flag
    cycle_stop_flag = True
    await message.answer('🛑 Цикл остановлен.')
    await state.set_state(BotState.idle)

@dp.message(F.text == '📜 История')
async def history_cmd(message: Message, state: FSMContext):
    if not history:
        await message.answer('История пустая.')
    else:
        await message.answer('\n'.join(history[-10:]))

@dp.message(F.text == '🔄 Сменить пару')
async def change_symbol_prompt(message: Message, state: FSMContext):
    await message.answer('Введите тикер монеты (например: BTC, ETH, SOL):')
    await state.set_state(BotState.waiting_for_symbol)

@dp.message(BotState.waiting_for_symbol)
async def set_symbol(message: Message, state: FSMContext):
    global ARKHAM_SYMBOL, BACKPACK_SYMBOL
    try:
        user_input = message.text.strip().upper()
        ARKHAM_SYMBOL = f'{user_input}_USDT_PERP'
        BACKPACK_SYMBOL = f'{user_input}_USDC'
        config.set('ARKHAM_SYMBOL', ARKHAM_SYMBOL)
        config.set('BACKPACK_SYMBOL', BACKPACK_SYMBOL)
        await message.answer(f'✅ Пара изменена:\nArkham: {ARKHAM_SYMBOL}\nBackpack: {BACKPACK_SYMBOL}')
        await state.set_state(BotState.idle)
    except Exception:
        await message.answer('❗ Ошибка ввода пары.')

@dp.message(F.text == '⚙️ Изменить Size')
async def change_size_prompt(message: Message, state: FSMContext):
    await message.answer('Введите новый размер позиции:')
    await state.set_state(BotState.waiting_for_size)

@dp.message(BotState.waiting_for_size)
async def set_size(message: Message, state: FSMContext):
    global size_position
    try:
        size_position = str(float(message.text.strip()))
        config.set('size_position', size_position)
        await message.answer(f'✅ Новый размер позиции установлен: {size_position}')
        await state.set_state(BotState.idle)
    except Exception:
        await message.answer('❗ Введите корректное число.')

# === Запуск ===
async def main():
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())