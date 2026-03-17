import os
import sys
import argparse
import threading
import random
import string
import asyncio
import shutil
import tempfile
import time
import re
import uuid
from flask import Flask
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.enums import ParseMode
import nyaa
import libtorrent as lt
import datetime

app = Flask(__name__)

@app.route("/")
def base_flask():
    return "Bot is running!"

def run_flask():
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)

search_cache = {}
download_tasks = {}
subscriptions = {}
subscription_lock = threading.Lock()

def generate_cache_id():
    return ''.join(random.choices(string.ascii_letters + string.digits, k=8))

def format_size(size):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size < 1024.0:
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} PB"

class TorrentDownloader:
    def __init__(self):
        self.active_downloads = {}
        self.downloads_lock = threading.Lock()
    
    def start_session(self):
        ses = lt.session()
        ses.listen_on(6881, 6891)
        ses.start_dht()
        return ses
    
    def add_torrent(self, ses, magnet_uri, save_path):
        params = {'save_path': save_path, 'storage_mode': lt.storage_mode_t.storage_mode_sparse}
        handle = lt.add_magnet_uri(ses, magnet_uri, params)
        return handle
    
    async def wait_for_metadata(self, handle):
        while not handle.has_metadata():
            await asyncio.sleep(1)
    
    async def download_magnet(self, magnet_link, save_path="downloads"):
        try:
            ses = self.start_session()
            handle = self.add_torrent(ses, magnet_link, save_path)
            await self.wait_for_metadata(handle)
            
            start_time = datetime.datetime.now()
            last_update = 0
            
            while handle.status().state != lt.torrent_status.seeding:
                s = handle.status()
                elapsed = datetime.datetime.now() - start_time
                elapsed_str = str(elapsed).split('.')[0]
                
                if s.state == lt.torrent_status.downloading:
                    progress = s.progress * 100
                    download_rate = s.download_rate / 1000
                    download_rate_mb = download_rate / 1024
                    current_mb = s.total_done / (1024 * 1024)
                    total_mb = s.total_wanted / (1024 * 1024)
                    bar_length = 20
                    filled_length = int(bar_length * progress / 100)
                    bar = "█" * filled_length + "▒" * (bar_length - filled_length)
                    torrent_name = handle.name() or "Descarga en curso"
                    
                    if time.time() - last_update > 2:
                        progress_msg = f"📥 Descargando: {torrent_name[:50]}...\n"
                        progress_msg += f"📊 Progreso: {progress:.2f}%\n"
                        progress_msg += f"📉 [{bar}]\n"
                        progress_msg += f"📦 Tamaño: {current_mb:.2f} MB / {total_mb:.2f} MB\n"
                        progress_msg += f"🚀 Velocidad: {download_rate_mb:.1f} MB/s\n"
                        progress_msg += f"⏱️ Tiempo: {elapsed_str}"
                        yield progress_msg
                        last_update = time.time()
                
                await asyncio.sleep(1)
            
            elapsed = datetime.datetime.now() - start_time
            elapsed_str = str(elapsed).split('.')[0]
            torrent_name = handle.name() or "unnamed"
            final_path = os.path.join(save_path, self.clean_name(torrent_name))
            
            if os.path.isfile(final_path):
                yield ("file", final_path)
            elif os.path.isdir(final_path):
                yield ("folder", final_path)
            else:
                yield ("error", "No se encontró el archivo descargado")
                
        except Exception as e:
            yield ("error", str(e))
    
    def clean_name(self, name):
        if not name:
            return "unnamed"
        prohibited_chars = '<>:"/\\|?*'
        cleaned = ''.join(c for c in name if c not in prohibited_chars)
        cleaned = cleaned.strip()
        while cleaned.endswith('.'):
            cleaned = cleaned[:-1].strip()
        if len(cleaned) > 248:
            cleaned = cleaned[:248]
        reserved_names = ['CON', 'PRN', 'AUX', 'NUL', 'COM1', 'COM2', 'COM3', 'COM4', 'COM5', 'COM6', 'COM7', 'COM8', 'COM9', 'LPT1', 'LPT2', 'LPT3', 'LPT4', 'LPT5', 'LPT6', 'LPT7', 'LPT8', 'LPT9']
        if cleaned.upper() in reserved_names:
            cleaned = '_' + cleaned
        if not cleaned:
            cleaned = "unnamed"
        return cleaned

downloader = TorrentDownloader()

class NekoTelegram:
    def __init__(self, api_id, api_hash, bot_token):
        self.api_id = api_id
        self.api_hash = api_hash
        self.bot_token = bot_token
        self.app = Client("nekobot", api_id=int(api_id), api_hash=api_hash, bot_token=bot_token)
        self.nyaa = nyaa.Nyaa_search()
        self.flask_thread = None
        
        @self.app.on_message(filters.private)
        async def handle_message(client: Client, message: Message):
            await self._handle_message(client, message)
        
        @self.app.on_callback_query()
        async def handle_callback(client: Client, callback_query: CallbackQuery):
            await self._handle_callback(client, callback_query)
    
    async def _handle_message(self, client: Client, message: Message):
        if not message.text:
            return
        
        text = message.text.strip()

        if text.startswith("/start"):
            await message.reply("Bot is running!\n\nComandos disponibles:\n/nyaa <búsqueda> - Buscar en Nyaa.si\n/nyaa18 <búsqueda> - Buscar en Sukebei\n/dl <magnet> - Descargar torrent\n/sub <URL> - Suscribirse a RSS")
        
        elif text.startswith("/nyaa "):
            query = text[6:].strip()
            await self._search_nyaa(client, message, query, False)
        
        elif text.startswith("/nyaa18 "):
            query = text[8:].strip()
            await self._search_nyaa(client, message, query, True)
        
        elif text.startswith("/dl "):
            magnet = text[4:].strip()
            await self._download_torrent(client, message, magnet)
        
        elif text.startswith("/sub "):
            url = text[5:].strip()
            await self._subscribe_rss(client, message, url)
    
    async def _search_nyaa(self, client: Client, message: Message, query: str, adult: bool):
        status_msg = await message.reply(f"🔍 Buscando: {query}...")
        
        try:
            if adult:
                results = self.nyaa.nyaafap(query)
            else:
                results = self.nyaa.nyaafun(query)
            
            if not results:
                await status_msg.edit_text("❌ No se encontraron resultados.")
                return
            
            cache_id = generate_cache_id()
            search_cache[cache_id] = results[:20]
            
            text = f"**Resultados para:** `{query}`\n\n"
            for i, result in enumerate(results[:5], 1):
                text += f"{i}. **{result['name'][:50]}**...\n"
                text += f"📦 {result['size']} | 📅 {result['date']}\n\n"
            
            keyboard = []
            for i, result in enumerate(results[:10], 1):
                keyboard.append([InlineKeyboardButton(f"{i}. {result['name'][:30]}...", callback_data=f"nyaa_detail_{cache_id}_{i-1}")])
            
            if len(results) > 5:
                total_pages = (len(results) + 4) // 5
                keyboard.append([
                    InlineKeyboardButton("⬅️ Anterior", callback_data=f"nyaa_page_{cache_id}_prev"),
                    InlineKeyboardButton(f"1/{total_pages}", callback_data="noop"),
                    InlineKeyboardButton("Siguiente ➡️", callback_data=f"nyaa_page_{cache_id}_next")
                ])
            
            await status_msg.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
            
        except Exception as e:
            await status_msg.edit_text(f"❌ Error: {str(e)}")
    
    async def _show_results_page(self, message: Message, cache_id: str, page: int):
        results = search_cache.get(cache_id, [])
        if not results:
            await message.edit_text("❌ Resultados expirados.")
            return
        
        total_pages = (len(results) + 4) // 5
        page = max(1, min(page, total_pages))
        
        start_idx = (page - 1) * 5
        end_idx = min(start_idx + 5, len(results))
        
        text = f"**Resultados (Página {page}/{total_pages})**\n\n"
        for i in range(start_idx, end_idx):
            result = results[i]
            text += f"**{i+1}.** {result['name'][:100]}\n"
            text += f"📦 {result['size']} | 📅 {result['date']}\n\n"
        
        keyboard = []
        for i in range(start_idx, end_idx):
            keyboard.append([InlineKeyboardButton(
                f"📥 {i+1}. {results[i]['name'][:30]}...", 
                callback_data=f"nyaa_detail_{cache_id}_{i}"
            )])
        
        nav_row = []
        if page > 1:
            nav_row.append(InlineKeyboardButton("⬅️ Anterior", callback_data=f"nyaa_page_{cache_id}_{page-1}"))
        else:
            nav_row.append(InlineKeyboardButton("⬅️ Anterior", callback_data="noop"))
        
        nav_row.append(InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"))
        
        if page < total_pages:
            nav_row.append(InlineKeyboardButton("Siguiente ➡️", callback_data=f"nyaa_page_{cache_id}_{page+1}"))
        else:
            nav_row.append(InlineKeyboardButton("Siguiente ➡️", callback_data="noop"))
        
        keyboard.append(nav_row)
        
        await message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    
    async def _download_torrent(self, client: Client, message: Message, magnet: str):
        if not magnet.startswith('magnet:'):
            await message.reply("❌ El enlace no parece ser un magnet válido.")
            return
        
        download_id = str(uuid.uuid4())[:8]
        status_msg = await message.reply(f"📥 Iniciando descarga...\nID: {download_id}")
        
        download_path = os.path.join(os.getcwd(), "downloads", download_id)
        os.makedirs(download_path, exist_ok=True)
        
        try:
            async for update in downloader.download_magnet(magnet, download_path):
                if isinstance(update, tuple):
                    if update[0] == "file":
                        file_path = update[1]
                        await status_msg.delete()
                        await message.reply_document(
                            document=file_path,
                            caption=f"✅ Descarga completada: {os.path.basename(file_path)}"
                        )
                        shutil.rmtree(download_path, ignore_errors=True)
                        break
                    elif update[0] == "folder":
                        folder_path = update[1]
                        await status_msg.delete()
                        
                        temp_zip = tempfile.NamedTemporaryFile(delete=False, suffix='.zip')
                        shutil.make_archive(temp_zip.name.replace('.zip', ''), 'zip', folder_path)
                        
                        await message.reply_document(
                            document=temp_zip.name,
                            caption=f"✅ Carpeta comprimida: {os.path.basename(folder_path)}.zip"
                        )
                        
                        os.unlink(temp_zip.name)
                        shutil.rmtree(download_path, ignore_errors=True)
                        break
                    elif update[0] == "error":
                        await status_msg.edit_text(f"❌ Error: {update[1]}")
                        break
                else:
                    await status_msg.edit_text(update)
                    
        except Exception as e:
            await status_msg.edit_text(f"❌ Error en descarga: {str(e)}")
            shutil.rmtree(download_path, ignore_errors=True)
    
    async def _subscribe_rss(self, client: Client, message: Message, url: str):
        sub_id = generate_cache_id()
        with subscription_lock:
            subscriptions[sub_id] = {
                'url': url,
                'user_id': message.from_user.id,
                'timestamp': time.time()
            }
        
        await message.reply(
            f"✅ Suscrito a RSS feed!\n\nID: {sub_id}\nURL: {url}",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancelar suscripción", callback_data=f"unsub_{sub_id}")
            ]])
        )
    
    async def _handle_callback(self, client: Client, callback_query: CallbackQuery):
        data = callback_query.data
        
        if data == "noop":
            await callback_query.answer()
            return
        
        if data.startswith("unsub_"):
            sub_id = data.replace("unsub_", "")
            with subscription_lock:
                if sub_id in subscriptions:
                    del subscriptions[sub_id]
                    await callback_query.message.edit_text("✅ Suscripción cancelada.")
                else:
                    await callback_query.answer("❌ Suscripción no encontrada")
            await callback_query.answer()
            return
        
        if data.startswith("nyaa_page_"):
            parts = data.split("_")
            cache_id = parts[2]
            
            if parts[3] == "prev" or parts[3] == "next":
                results = search_cache.get(cache_id, [])
                if not results:
                    await callback_query.answer("Resultados expirados")
                    return
                
                current_text = callback_query.message.text
                current_page = 1
                
                page_match = re.search(r'Página (\d+)/(\d+)', current_text)
                if page_match:
                    current_page = int(page_match.group(1))
                    total_pages = int(page_match.group(2))
                else:
                    total_pages = (len(results) + 4) // 5
                
                if parts[3] == "next":
                    new_page = current_page + 1
                else:
                    new_page = current_page - 1
                
                if 1 <= new_page <= total_pages:
                    await self._show_results_page(callback_query.message, cache_id, new_page)
            else:
                page = int(parts[3])
                await self._show_results_page(callback_query.message, cache_id, page)
            
            await callback_query.answer()
            
        elif data.startswith("nyaa_detail_"):
            parts = data.split("_")
            cache_id = parts[2]
            idx = int(parts[3])
            
            results = search_cache.get(cache_id, [])
            if not results or idx >= len(results):
                await callback_query.answer("Resultado no encontrado", show_alert=True)
                return
            
            result = results[idx]
            
            text = f"**{result['name']}**\n\n"
            text += f"📦 Tamaño: {result['size']}\n"
            text += f"📅 Fecha: {result['date']}\n\n"
            
            download_id = generate_cache_id()
            download_tasks[download_id] = (result['magnet'], result['name'])
            
            keyboard = []
            if result.get('magnet'):
                keyboard.append([InlineKeyboardButton("🧲 Magnet", url=result['magnet'])])
            if result.get('torrent'):
                keyboard.append([InlineKeyboardButton("⬇️ Torrent", url=result['torrent'])])
            
            keyboard.append([InlineKeyboardButton("📥 Descargar ahora", callback_data=f"download_{download_id}")])
            keyboard.append([InlineKeyboardButton("🔙 Volver", callback_data=f"nyaa_page_{cache_id}_1")])
            
            await callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
            await callback_query.answer()
            
        elif data.startswith("download_"):
            download_id = data.replace("download_", "")
            
            if download_id in download_tasks:
                magnet, name = download_tasks[download_id]
                
                await callback_query.message.edit_text(f"📥 Iniciando descarga: {name}...")
                
                download_path = os.path.join(os.getcwd(), "downloads", download_id)
                os.makedirs(download_path, exist_ok=True)
                
                try:
                    status_msg = await callback_query.message.reply("⏳ Iniciando descarga...")
                    
                    async for update in downloader.download_magnet(magnet, download_path):
                        if isinstance(update, tuple):
                            if update[0] == "file":
                                file_path = update[1]
                                await status_msg.delete()
                                await callback_query.message.reply_document(
                                    document=file_path,
                                    caption=f"✅ Descarga completada: {os.path.basename(file_path)}"
                                )
                                shutil.rmtree(download_path, ignore_errors=True)
                                break
                            elif update[0] == "folder":
                                folder_path = update[1]
                                await status_msg.delete()
                                
                                temp_zip = tempfile.NamedTemporaryFile(delete=False, suffix='.zip')
                                shutil.make_archive(temp_zip.name.replace('.zip', ''), 'zip', folder_path)
                                
                                await callback_query.message.reply_document(
                                    document=temp_zip.name,
                                    caption=f"✅ Carpeta comprimida: {os.path.basename(folder_path)}.zip"
                                )
                                
                                os.unlink(temp_zip.name)
                                shutil.rmtree(download_path, ignore_errors=True)
                                break
                            elif update[0] == "error":
                                await status_msg.edit_text(f"❌ Error: {update[1]}")
                                break
                        else:
                            await status_msg.edit_text(update)
                            
                except Exception as e:
                    await callback_query.message.reply(f"❌ Error en descarga: {str(e)}")
                    shutil.rmtree(download_path, ignore_errors=True)
                
                del download_tasks[download_id]
            
            await callback_query.answer()
    
    def start_flask(self):
        if self.flask_thread and self.flask_thread.is_alive():
            return
            
        self.flask_thread = threading.Thread(target=run_flask, daemon=True)
        self.flask_thread.start()
    
    def run(self):
        self.app.run()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-A", "--api", help="API ID")
    parser.add_argument("-H", "--hash", help="API Hash")
    parser.add_argument("-T", "--token", help="Bot Token")
    parser.add_argument("-F", "--flask", action="store_true", help="Incluir Flask")
    args = parser.parse_args()

    api_id = args.api or os.environ.get("API_ID")
    api_hash = args.hash or os.environ.get("API_HASH")
    bot_token = args.token or os.environ.get("BOT_TOKEN")
    
    if not all([api_id, api_hash, bot_token]):
        print("Error: Faltan credenciales")
        sys.exit(1)
    
    os.makedirs("downloads", exist_ok=True)
    
    bot = NekoTelegram(api_id, api_hash, bot_token)

    if args.flask:
        bot.start_flask()
    
    bot.run()

if __name__ == "__main__":
    main()
