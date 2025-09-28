import sqlite3
import logging
import json
import io
import asyncio
import textwrap
import os
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont
from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    BufferedInputFile,
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv
load_dotenv()  

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = [1346805322, 7131973974] 
DB_FILE = "lacore_simple_v2.db"
JSON_FILE = "products.json"

storage = MemoryStorage()
dp = Dispatcher(storage=storage)
bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="HTML"))


# --- 3. MA'LUMOTLAR BAZASI (DB) ---
class Database:
    def __init__(self, db_file):
        self.connection = sqlite3.connect(db_file)
        self.cursor = self.connection.cursor()

    def init_db(self):
        self.cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS products (
                kod TEXT PRIMARY KEY, category TEXT NOT NULL, name_ru TEXT NOT NULL,
                name_en TEXT NOT NULL, internal_price REAL NOT NULL, 
                external_price REAL NOT NULL, points REAL NOT NULL, volume REAL
            )"""
        )
        self.connection.commit()

    def sync_products_from_json(self):
        with open(JSON_FILE, "r", encoding="utf-8") as f: data = json.load(f)["mahsulotlar"]
        products_to_sync = [(p['kod'], c, p['nom']['ru'], p['nom']['en'], p['chegirma_narx'], p['narx'], p['ball'], p['hajm']) 
                            for c, p_list in data.items() for p in p_list]
        self.cursor.executemany("INSERT OR REPLACE INTO products VALUES (?, ?, ?, ?, ?, ?, ?, ?)", products_to_sync)
        self.connection.commit()
        logging.info(f"{len(products_to_sync)} ta mahsulot DB bilan sinxronlandi.")

    def get_product_details(self, product_kod):
        self.cursor.execute("SELECT name_ru, name_en, internal_price, external_price, points, volume FROM products WHERE kod = ?", (product_kod,))
        return self.cursor.fetchone()

db = Database(DB_FILE)


# --- 4. YORDAMCHI FUNKSIYALAR VA KLASSLAR ---
def create_report_image(salesperson_name, report_date, report_data, grand_total_sum, grand_total_points):
    """Hisobot uchun rasm-jadval yasaydi (SANA QO'SHILGAN)"""
    base_row_height = 35; width = 1100
    try:
        title_font = ImageFont.truetype("arialbd.ttf", 32); header_font = ImageFont.truetype("arialbd.ttf", 18); text_font = ImageFont.truetype("arial.ttf", 15)
    except IOError:
        title_font, header_font, text_font = ImageFont.load_default(), ImageFont.load_default(), ImageFont.load_default()

    dynamic_height = 330 # Balandlikka sana uchun joy qo'shildi
    for _, name, _, _, _ in report_data:
        dynamic_height += base_row_height * len(textwrap.wrap(name, width=60))

    img = Image.new("RGB", (width, dynamic_height), color=(255, 255, 255))
    draw = ImageDraw.Draw(img); text_color = (0, 0, 0)

    draw.text((width / 2, 50), "Sotuv Hisoboti", font=title_font, fill=text_color, anchor="ms")
    draw.text((50, 110), f"Sotuvchi: {salesperson_name}", font=header_font, fill=text_color)
    draw.text((50, 140), f"Sana: {report_date}", font=header_font, fill=text_color) # SANA QO'SHILDI
    draw.line([(50, 180), (width - 50, 180)], fill=text_color, width=2)

    y = 200
    headers = {"Kod": 50, "Mahsulot": 150, "Soni": 700, "Narxi": 830, "Jami Ball": 980}
    for header, x_pos in headers.items(): draw.text((x_pos, y), header, font=header_font, fill=text_color)
    
    y += 40
    for kod, name, qty, price, total_points in report_data:
        wrapped_lines = textwrap.wrap(name, width=60)
        line_count = len(wrapped_lines)
        vertical_offset = (base_row_height * line_count) / 2
        
        draw.text((headers["Kod"], y + vertical_offset - 8), kod, font=text_font, fill=text_color)
        draw.text((headers["Soni"], y + vertical_offset - 8), f"{qty} dona", font=text_font, fill=text_color)
        draw.text((headers["Narxi"], y + vertical_offset - 8), f"{price:,.0f} so'm", font=text_font, fill=text_color)
        draw.text((headers["Jami Ball"], y + vertical_offset - 8), f"{total_points:.1f}", font=text_font, fill=text_color)

        text_y = y
        for line in wrapped_lines:
            draw.text((headers["Mahsulot"], text_y), line, font=text_font, fill=text_color)
            text_y += 20
        y += base_row_height * line_count

    draw.line([(50, y), (width - 50, y)], fill=text_color, width=2)
    y += 20
    draw.text((50, y), f"UMUMIY TUSHUM: {grand_total_sum:,.0f} so'm", font=header_font, fill=text_color)
    y += 30
    draw.text((50, y), f"UMUMIY BALL: {grand_total_points:.1f}", font=header_font, fill=text_color)

    img_byte_arr = io.BytesIO()
    img.save(img_byte_arr, format="PNG")
    return img_byte_arr.getvalue()

# TUGMALAR (KEYBOARDS)
def main_menu():
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✍️ Sotuv qo'shish", callback_data="add_sale"),
        InlineKeyboardButton(text="🛍️ Mahsulotlar", callback_data="view_products"),
    )
    return builder.as_markup()

def stop_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Tugatish va Hisobotni Yaratish", callback_data="stop_adding_products")
    ]])

def quantity_keyboard():
    """Mahsulot sonini kiritish uchun 1-10 gacha tugmalar yasaydi"""
    builder = InlineKeyboardBuilder()
    builder.row(*[InlineKeyboardButton(text=str(i), callback_data=f"qty_{i}") for i in range(1, 6)])
    builder.row(*[InlineKeyboardButton(text=str(i), callback_data=f"qty_{i}") for i in range(6, 11)])
    return builder.as_markup()

# HOLATLAR (STATES)
class ViewProduct(StatesGroup): get_code = State()
class GenerateReport(StatesGroup):
    get_salesperson_name = State()
    get_product_code = State()
    get_product_quantity = State()

# --- 5. HANDLERLAR ---

@dp.message(Command("start"), StateFilter("*"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    if message.from_user.id in ADMIN_ID:
        await message.answer(f"Assalomu alaykum, <b>{message.from_user.full_name}</b>!", reply_markup=main_menu())
    else:
        await message.answer("Assalomu alaykum! Bu bot faqat admin uchun.")

@dp.callback_query(F.data == "main_menu", StateFilter("*"))
async def back_to_main_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("Bosh menyu", reply_markup=main_menu())

# --- MAHSULOTLARNI KOD ORQALI KO'RISH ---
@dp.callback_query(F.data == "view_products")
async def view_products_start(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("Mahsulot ma'lumotini ko'rish uchun uning kodini yuboring:")
    await state.set_state(ViewProduct.get_code)

@dp.message(StateFilter(ViewProduct.get_code))
async def show_product_by_code(message: Message, state: FSMContext):
    product_kod = message.text
    details = db.get_product_details(product_kod)
    await state.clear()
    if details:
        name_ru, name_en, internal, external, points, volume = details
        text = (f"<b>Mahsulot (RU):</b> {name_ru}\n"
                f"<b>Product (EN):</b> {name_en}\n"
                f"(kod: {product_kod})\n\n"
                f"<b>Hajmi:</b> {volume} ml\n"
                f"<b>Ichki narx:</b> {internal:,.0f} so'm\n"
                f"<b>Tashqi narx:</b> {external:,.0f} so'm\n"
                f"<b>Ball:</b> {points} ⭐")
        await message.answer(text, reply_markup=main_menu())
    else:
        await message.answer("❌ Bunday kodli mahsulot topilmadi.", reply_markup=main_menu())

# --- SOTUV QO'SHISH VA HISOBOT YARATISH ---
@dp.callback_query(F.data == "add_sale")
async def add_sale_start(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("Sotuvchi (yoki xaridor) ism-sharifini kiriting:")
    await state.set_state(GenerateReport.get_salesperson_name)

@dp.message(StateFilter(GenerateReport.get_salesperson_name))
async def process_salesperson_name(message: Message, state: FSMContext):
    await state.update_data(salesperson_name=message.text, products=[])
    await message.answer("Rahmat. Endi birinchi mahsulot kodini kiriting:", reply_markup=stop_keyboard())
    await state.set_state(GenerateReport.get_product_code)

@dp.message(StateFilter(GenerateReport.get_product_code))
async def process_product_code(message: Message, state: FSMContext):
    product_kod = message.text
    product_details = db.get_product_details(product_kod)
    
    if not product_details:
        return await message.answer("❌ Bunday kodli mahsulot topilmadi. Boshqa kod kiriting yoki tugatish uchun pastdagi tugmani bosing.",
                                    reply_markup=stop_keyboard())
    
    name_ru, name_en, _, _, _, _ = product_details
    await state.update_data(current_kod=product_kod)
    # Endi sonini so'raymiz va tugmalarni yuboramiz
    await message.answer(f"✅ Mahsulot topildi:\n<b>{name_ru}\n{name_en}</b>\n\nEndi shu mahsulotdan necha dona sotilganini tanlang:",
                         reply_markup=quantity_keyboard())
    await state.set_state(GenerateReport.get_product_quantity)

@dp.callback_query(F.data.startswith("qty_"), StateFilter(GenerateReport.get_product_quantity))
async def process_product_quantity_callback(call: CallbackQuery, state: FSMContext):
    # Oldingi xabarni (sonlar yozilgan tugmalarni) o'chirib tashlaymiz
    await call.message.delete()
    
    quantity = int(call.data.split("_")[1])
    
    data = await state.get_data()
    products_list = data.get("products", [])
    current_kod = data.get("current_kod")
    
    products_list.append((current_kod, quantity))
    await state.update_data(products=products_list)
    
    name_ru, name_en, _, _, _, _ = db.get_product_details(current_kod)
    await call.message.answer(f"✅ Qo'shildi: <b>{name_ru} / {name_en}</b> - {quantity} dona.\n\nNavbatdagi mahsulot kodini kiriting:",
                         reply_markup=stop_keyboard())
    await state.set_state(GenerateReport.get_product_code)

@dp.callback_query(F.data == "stop_adding_products", StateFilter(GenerateReport.get_product_code))
async def stop_and_generate_report(call: CallbackQuery, state: FSMContext):
    await call.message.delete()
    data = await state.get_data()
    salesperson_name = data.get("salesperson_name")
    products_list = data.get("products", [])

    if not products_list:
        await call.message.answer("Hech qanday mahsulot qo'shilmadi.", reply_markup=main_menu())
        await state.clear()
        return
        
    await call.message.answer("Hisobot tayyorlanmoqda, iltimos kuting...")

    report_data = []; grand_total_sum = 0; grand_total_points = 0;
    current_date = datetime.now().strftime('%d.%m.%Y')

    for kod, qty in products_list:
        details = db.get_product_details(kod)
        name_ru, name_en, internal_price, _, points, _ = details
        total_price = internal_price * qty; total_points = points * qty
        combined_name = f"{name_ru} / {name_en}"
        report_data.append((kod, combined_name, qty, internal_price, total_points))
        grand_total_sum += total_price; grand_total_points += total_points
        
    text_report = f"Sotuvchi: <b>{salesperson_name}</b>\nSana: {current_date}\n"
    text_report += "<b>Hisob-kitob ICHKI NARXDA amalga oshirildi!</b>\n\n"
    for kod, name, qty, price, points in report_data:
        text_report += f"🔹 ({kod}) {name}\n   {qty} dona x {price:,.0f} = {(qty*price):,.0f} so'm ({points:.1f} ball)\n"
    
    text_report += f"\n------------------------------------\n"
    text_report += f"📈 <b>Jami Tushum:</b> {grand_total_sum:,.0f} so'm\n"
    text_report += f"🏆 <b>Jami Ball:</b> {grand_total_points:.1f}"

    report_image_bytes = create_report_image(salesperson_name, current_date, report_data, grand_total_sum, grand_total_points)

    await bot.send_photo(
        chat_id=call.from_user.id,
        photo=BufferedInputFile(report_image_bytes, filename=f"report.png"),
        caption=text_report
    )
    await call.message.answer("Yangi hisobot uchun bosh menyuga qayting.", reply_markup=main_menu())
    await state.clear()

# --- 6. BOTNI ISHGA TUSHIRISH ---
async def main():
    logging.basicConfig(level=logging.INFO)
    db.init_db()
    db.sync_products_from_json()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())