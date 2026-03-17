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
import uuid
import schedule
from flask import Flask
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
import nyaa
import libtorrent as lt
import datetime
import queue
from concurrent.futures import ThreadPoolExecutor

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
notification_queue = queue.Queue()

def generate_cache_id():
    return ''.join(random.choices(string.ascii_letters + string.digits, k=8))

class TorrentDownloader:
    def __init__(self):
        self.active_downloads = {}
        self.downloads_lock = threading.Lock()
        self.executor = ThreadPoolExecutor(max_workers=3)
    
    def start_session(self):
        ses = lt.session()
        ses.listen_on(6881, 6891)
        ses.start_dht()
        return ses
    
    def add_torrent(self, ses, magnet_uri, save_path):
        params = {
            'save_path': save_path,
            'storage_mode': lt.storage_mode_t.storage_mode_sparse,
            'auto_managed': True,
            'duplicate_is_error': True
        }
        
        if magnet_uri.startswith('magnet:'):
            handle = lt.add_magnet_uri(ses, magnet_uri, params)
        else:
            handle = lt.add_torrent_file(ses, magnet_uri, params)
        
        return handle
    
    async def wait_for_metadata(self, handle):
        max_wait = 30
        waited = 0
        while not handle.has_metadata() and waited < max_wait:
            await asyncio.sleep(1)
            waited += 1
        return handle.has_metadata()
    
    async def download_magnet(self, magnet_link, save_path="downloads", multiple_files=False):
        try:
            ses = self.start_session()
            handle = self.add_torrent(ses, magnet_link, save_path)
            
            has_metadata = await self.wait_for_metadata(handle)
            if not has_metadata:
                yield ("error", "No se pudo obtener metadata del torrent")
                return
            
            start_time = datetime.datetime.now()
            last_update = 0
            last_progress = -1
            
            while handle.status().state != lt.torrent_status.seeding:
                s = handle.status()
                
                if s.errc:
                    yield ("error", f"Error en torrent: {s.errc.message()}")
                    return
                
                elapsed = datetime.datetime.now() - start_time
                elapsed_str = str(elapsed).split('.')[0]
                
                if s.state == lt.torrent_status.downloading:
                    progress = s.progress * 100
                    
                    if abs(progress - last_progress) >= 1 or time.time() - last_update > 5:
                        download_rate = s.download_rate / 1000
                        download_rate_mb = download_rate / 1024
                        current_mb = s.total_done / (1024 * 1024)
                        total_mb = s.total_wanted / (1024 * 1024)
                        bar_length = 20
                        filled_length = int(bar_length * progress / 100)
                        bar = "█" * filled_length + "▒" * (bar_length - filled_length)
                        torrent_name = handle.name() or "Descarga en curso"
                        
                        progress_msg = f"📥 Descargando: {torrent_name[:50]}...\n"
                        progress_msg += f"📊 Progreso: {progress:.2f}%\n"
                        progress_msg += f"📉 [{bar}]\n"
                        progress_msg += f"📦 Tamaño: {current_mb:.2f} MB / {total_mb:.2f} MB\n"
                        progress_msg += f"🚀 Velocidad: {download_rate_mb:.1f} MB/s\n"
                        progress_msg += f"⏱️ Tiempo: {elapsed_str}"
                        yield progress_msg
                        
                        last_update = time.time()
                        last_progress = progress
                
                await asyncio.sleep(1)
            
            torrent_name = handle.name() or "unnamed"
            final_path = os.path.join(save_path, self.clean_name(torrent_name))
            
            await asyncio.sleep(2)
            
            if os.path.isfile(final_path):
                yield ("file", final_path)
            elif os.path.isdir(final_path):
                files_to_send = []
                for root, dirs, files in os.walk(final_path):
                    for file in files:
                        file_path = os.path.join(root, file)
                        files_to_send.append(file_path)
                
                if multiple_files and len(files_to_send) > 1:
                    yield ("multiple_files", files_to_send)
                elif files_to_send:
                    yield ("file", files_to_send[0])
                else:
                    yield ("error", "No se encontraron archivos")
            else:
                found_files = []
                for root, dirs, files in os.walk(save_path):
                    for file in files:
                        if file.endswith(('.mkv', '.mp4', '.avi', '.mov', '.mp3', '.flac', '.zip', '.rar', '.pdf')):
                            found_files.append(os.path.join(root, file))
                
                if found_files:
                    if multiple_files and len(found_files) > 1:
                        yield ("multiple_files", found_files)
                    else:
                        yield ("file", found_files[0])
                else:
                    yield ("error", "No se encontró el archivo descargado")
                
        except Exception as e:
            yield ("error", f"Error en descarga: {str(e)}")
    
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

def subscription_worker():
    while True:
        try:
            schedule.run_pending()
            time.sleep(1)
        except Exception as e:
            print(f"Error en subscription_worker: {e}")
            time.sleep(5)

def check_subscriptions():
    global subscriptions
    
    with subscription_lock:
        subs_copy = dict(subscriptions)
    
    for sub_id, sub_data in subs_copy.items():
        try:
            chat_id = sub_data['chat_id']
            query = sub_data['query']
            adult = sub_data['adult']
            last_result = sub_data.get('last_result')
            
            print(f"Verificando suscripción: {query} para chat {chat_id}")
            
            nyaa_search = nyaa.Nyaa_search()
            if adult:
                results = nyaa_search.nyaafap(query)
            else:
                results = nyaa_search.nyaafun(query)
            
            if results and len(results) > 0:
                first_result = results[0]
                
                is_new = False
                if last_result is None:
                    is_new = True
                elif first_result.get('date') != last_result.get('date'):
                    is_new = True
                elif first_result.get('name') != last_result.get('name'):
                    is_new = True
                
                if is_new:
                    print(f"Nuevo resultado encontrado para {query}")
                    
                    with subscription_lock:
                        if sub_id in subscriptions:
                            subscriptions[sub_id]['last_result'] = first_result
                    
                    download_link = None
                    if first_result.get('magnet'):
                        download_link = first_result['magnet']
                    elif first_result.get('torrent'):
                        download_link = first_result['torrent']
                    
                    if download_link:
                        text = f"🆕 **Nuevo resultado encontrado!**\n\n"
                        text += f"**Búsqueda:** `{query}`\n"
                        text += f"**{first_result['name']}**\n"
                        text += f"📦 Tamaño: {first_result['size']}\n"
                        text += f"📅 Fecha: {first_result['date']}\n\n"
                        text += "Iniciando descarga automática..."
                        
                        notification_queue.put({
                            'chat_id': chat_id,
                            'text': text,
                            'download_link': download_link
                        })
                        
        except Exception as e:
            print(f"Error checking subscription {sub_id}: {e}")

class NekoTelegram:
    def __init__(self, api_id, api_hash, bot_token):
        self.api_id = api_id
        self.api_hash = api_hash
        self.bot_token = bot_token
        self.app = Client("nekobot", api_id=int(api_id), api_hash=api_hash, bot_token=bot_token)
        self.nyaa = nyaa.Nyaa_search()
        self.flask_thread = None
        self.subscription_thread = None
        self.notification_thread = None
        self.running = True
        
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
            await message.reply("Bot is running!\n\nComandos disponibles:\n/nyaa <búsqueda> - Buscar en Nyaa.si\n/nyaa18 <búsqueda> - Buscar en Sukebei\n/dl <magnet> - Descargar torrent\n/sub <término> - Suscribirse a Nyaa\n/sub18 <término> - Suscribirse a Sukebei\n/rmsub <término> - Eliminar suscripción Nyaa\n/rmsub18 <término> - Eliminar suscripción Sukebei\n/misubs - Ver suscripciones\n/limpiar - Limpiar descargas completadas")
        
        elif text.startswith("/nyaa "):
            query = text[6:].strip()
            await self._search_nyaa(client, message, query, False)
        
        elif text.startswith("/nyaa18 "):
            query = text[8:].strip()
            await self._search_nyaa(client, message, query, True)
        
        elif text.startswith("/dl "):
            magnet = text[4:].strip()
            await self._download_torrent(client, message, magnet, multiple_files=True)
        
        elif text.startswith("/sub "):
            query = text[5:].strip()
            await self._add_subscription(client, message, query, False)
        
        elif text.startswith("/sub18 "):
            query = text[7:].strip()
            await self._add_subscription(client, message, query, True)
        
        elif text.startswith("/rmsub "):
            query = text[7:].strip()
            await self._remove_subscription(client, message, query, False)
        
        elif text.startswith("/rmsub18 "):
            query = text[9:].strip()
            await self._remove_subscription(client, message, query, True)
        
        elif text == "/misubs":
            await self._list_subscriptions(client, message)
        
        elif text == "/limpiar":
            await self._clean_downloads(client, message)
    
    async def _add_subscription(self, client: Client, message: Message, query: str, adult: bool):
        status_msg = await message.reply(f"🔍 Configurando suscripción para: {query}...")
        
        try:
            if adult:
                results = self.nyaa.nyaafap(query)
            else:
                results = self.nyaa.nyaafun(query)
            
            if not results:
                await status_msg.edit_text("❌ No se encontraron resultados para esta búsqueda.")
                return
            
            first_result = results[0]
            
            sub_id = generate_cache_id()
            
            with subscription_lock:
                subscriptions[sub_id] = {
                    'chat_id': message.chat.id,
                    'query': query,
                    'adult': adult,
                    'last_result': first_result,
                    'created_at': time.time()
                }
            
            text = f"✅ **Suscripción activada**\n\n"
            text += f"**Término:** `{query}`\n"
            text += f"**Modo:** {'+18' if adult else 'Normal'}\n\n"
            text += f"**Último resultado guardado:**\n"
            text += f"**{first_result['name'][:100]}**\n"
            text += f"📦 {first_result['size']} | 📅 {first_result['date']}\n\n"
            text += "El bot revisará cada minuto y descargará automáticamente nuevos resultados."
            
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Cancelar suscripción", callback_data=f"sub_remove_{sub_id}")]
            ])
            
            await status_msg.edit_text(text, reply_markup=keyboard)
            
        except Exception as e:
            await status_msg.edit_text(f"❌ Error: {str(e)}")
    
    async def _remove_subscription(self, client: Client, message: Message, query: str, adult: bool):
        removed = False
        with subscription_lock:
            for sub_id, sub_data in list(subscriptions.items()):
                if sub_data['chat_id'] == message.chat.id and sub_data['query'] == query and sub_data['adult'] == adult:
                    del subscriptions[sub_id]
                    removed = True
                    break
        
        if removed:
            await message.reply(f"✅ Suscripción eliminada para: `{query}`")
        else:
            await message.reply(f"❌ No se encontró suscripción para: `{query}`")
    
    async def _list_subscriptions(self, client: Client, message: Message):
        user_subs = []
        with subscription_lock:
            for sub_id, sub_data in subscriptions.items():
                if sub_data['chat_id'] == message.chat.id:
                    user_subs.append((sub_id, sub_data))
        
        if not user_subs:
            await message.reply("📭 No tienes suscripciones activas.")
            return
        
        text = "**📋 Tus suscripciones:**\n\n"
        keyboard = []
        for sub_id, sub_data in user_subs:
            text += f"**Término:** `{sub_data['query']}`\n"
            text += f"**Modo:** {'+18' if sub_data['adult'] else 'Normal'}\n"
            if sub_data['last_result']:
                text += f"**Último:** {sub_data['last_result']['name'][:50]}...\n"
                text += f"**Fecha:** {sub_data['last_result']['date']}\n"
            text += f"ID: `{sub_id}`\n\n"
            keyboard.append([InlineKeyboardButton(f"❌ {sub_data['query']}", callback_data=f"sub_remove_{sub_id}")])
        
        await message.reply(text, reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None)
    
    async def _clean_downloads(self, client: Client, message: Message):
        download_dir = "downloads"
        if os.path.exists(download_dir):
            count = 0
            for item in os.listdir(download_dir):
                item_path = os.path.join(download_dir, item)
                if os.path.isdir(item_path):
                    try:
                        shutil.rmtree(item_path)
                        count += 1
                    except:
                        pass
            await message.reply(f"✅ Limpiadas {count} carpetas de descarga")
        else:
            await message.reply("📂 No hay carpetas de descarga")
    
    async def _process_notifications(self):
        while self.running:
            try:
                while not notification_queue.empty():
                    notification = notification_queue.get_nowait()
                    try:
                        msg = await self.app.send_message(
                            notification['chat_id'], 
                            notification['text']
                        )
                        await self._download_torrent(
                            self.app, 
                            msg, 
                            notification['download_link'],
                            multiple_files=True
                        )
                    except Exception as e:
                        print(f"Error sending notification: {e}")
                
                await asyncio.sleep(1)
            except Exception as e:
                print(f"Error in notification processor: {e}")
                await asyncio.sleep(5)
    
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
            search_cache[cache_id] = results
            
            await self._show_results_page(status_msg, cache_id, 1)
            
        except Exception as e:
            await status_msg.edit_text(f"❌ Error: {str(e)}")
    
    async def _show_results_page(self, message: Message, cache_id: str, page: int):
        results = search_cache.get(cache_id)
        if not results:
            await message.edit_text("❌ Resultados no encontrados.")
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
        
        keyboard.append([InlineKeyboardButton(
            f"📥 DESCARGAR TODOS ({end_idx-start_idx})", 
            callback_data=f"nyaa_dl_all_{cache_id}_{start_idx}_{end_idx}"
        )])
        
        nav_row = []
        if page > 1:
            nav_row.append(InlineKeyboardButton("⬅️ Anterior", callback_data=f"nyaa_page_{cache_id}_{page-1}"))
        else:
            nav_row.append(InlineKeyboardButton("⬅️ Anterior", callback_data="noop"))
        
        nav_row.append(InlineKeyboardButton(f"📄 {page}/{total_pages}", callback_data="noop"))
        
        if page < total_pages:
            nav_row.append(InlineKeyboardButton("Siguiente ➡️", callback_data=f"nyaa_page_{cache_id}_{page+1}"))
        else:
            nav_row.append(InlineKeyboardButton("Siguiente ➡️", callback_data="noop"))
        
        keyboard.append(nav_row)
        
        await message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    
    async def _download_torrent(self, client: Client, message: Message, link: str, multiple_files=False):
        if not (link.startswith('magnet:') or link.endswith('.torrent')):
            await message.reply("❌ El enlace no parece ser un magnet o torrent válido.")
            return
        
        download_id = str(uuid.uuid4())[:8]
        status_msg = await message.reply(f"📥 Iniciando descarga...\nID: {download_id}")
        
        download_path = os.path.join(os.getcwd(), "downloads", download_id)
        os.makedirs(download_path, exist_ok=True)
        
        try:
            async for update in downloader.download_magnet(link, download_path, multiple_files):
                if isinstance(update, tuple):
                    if update[0] == "file":
                        file_path = update[1]
                        await status_msg.delete()
                        
                        file_size = os.path.getsize(file_path)
                        if file_size > 50 * 1024 * 1024:
                            await message.reply(
                                f"✅ Descarga completada: {os.path.basename(file_path)}\n"
                                f"📦 Tamaño: {file_size / (1024*1024):.2f} MB\n"
                                f"⚠️ Archivo muy grande para enviar por Telegram"
                            )
                        else:
                            await message.reply_document(
                                document=file_path,
                                caption=f"✅ Descarga completada: {os.path.basename(file_path)}"
                            )
                        
                        try:
                            os.remove(file_path)
                            shutil.rmtree(download_path, ignore_errors=True)
                        except:
                            pass
                        break
                        
                    elif update[0] == "multiple_files":
                        files = update[1]
                        await status_msg.delete()
                        
                        await message.reply(f"📦 Enviando {len(files)} archivos individualmente...")
                        
                        for file_path in files:
                            file_size = os.path.getsize(file_path)
                            if file_size > 50 * 1024 * 1024:
                                await message.reply(
                                    f"📁 {os.path.basename(file_path)}\n"
                                    f"📦 Tamaño: {file_size / (1024*1024):.2f} MB\n"
                                    f"⚠️ Archivo muy grande para enviar"
                                )
                            else:
                                try:
                                    await message.reply_document(
                                        document=file_path,
                                        caption=f"📁 {os.path.basename(file_path)}"
                                    )
                                except Exception as e:
                                    await message.reply(f"❌ Error enviando {os.path.basename(file_path)}: {str(e)}")
                            
                            await asyncio.sleep(1)
                        
                        await message.reply("✅ Todos los archivos enviados!")
                        
                        try:
                            shutil.rmtree(download_path, ignore_errors=True)
                        except:
                            pass
                        break
                        
                    elif update[0] == "error":
                        await status_msg.edit_text(f"❌ Error: {update[1]}")
                        shutil.rmtree(download_path, ignore_errors=True)
                        break
                else:
                    await status_msg.edit_text(update)
                    
        except Exception as e:
            await status_msg.edit_text(f"❌ Error en descarga: {str(e)}")
            shutil.rmtree(download_path, ignore_errors=True)
    
    async def _handle_callback(self, client: Client, callback_query: CallbackQuery):
        data = callback_query.data
        
        if data == "noop":
            await callback_query.answer()
            return
        
        if data.startswith("sub_remove_"):
            sub_id = data[11:]
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
            page = int(parts[3])
            
            await self._show_results_page(callback_query.message, cache_id, page)
            await callback_query.answer()
            
        elif data.startswith("nyaa_detail_"):
            parts = data.split("_")
            cache_id = parts[2]
            index = int(parts[3])
            
            results = search_cache.get(cache_id)
            if not results or index >= len(results):
                await callback_query.answer("❌ Resultado no encontrado", show_alert=True)
                return
            
            result = results[index]
            
            text = f"**{result['name']}**\n\n"
            text += f"📦 **Tamaño:** {result['size']}\n"
            text += f"📅 **Fecha:** {result['date']}\n\n"
            
            keyboard = []
            
            if result.get('magnet'):
                keyboard.append([InlineKeyboardButton("🧲 Descargar Magnet", callback_data=f"nyaa_dl_magnet_{cache_id}_{index}")])
            
            if result.get('torrent'):
                keyboard.append([InlineKeyboardButton("⬇️ Descargar Torrent", callback_data=f"nyaa_dl_torrent_{cache_id}_{index}")])
            
            keyboard.append([InlineKeyboardButton("🔙 Volver", callback_data=f"nyaa_page_{cache_id}_1")])
            
            await callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
            await callback_query.answer()
            
        elif data.startswith("nyaa_dl_magnet_"):
            parts = data.split("_")
            cache_id = parts[3]
            index = int(parts[4])
            
            results = search_cache.get(cache_id)
            if not results or index >= len(results):
                await callback_query.answer("❌ Resultado no encontrado", show_alert=True)
                return
            
            magnet = results[index].get('magnet')
            if magnet:
                await callback_query.answer("Iniciando descarga...")
                await self._download_torrent(client, callback_query.message, magnet, multiple_files=True)
            else:
                await callback_query.answer("❌ No hay magnet disponible", show_alert=True)
                
        elif data.startswith("nyaa_dl_torrent_"):
            parts = data.split("_")
            cache_id = parts[3]
            index = int(parts[4])
            
            results = search_cache.get(cache_id)
            if not results or index >= len(results):
                await callback_query.answer("❌ Resultado no encontrado", show_alert=True)
                return
            
            torrent = results[index].get('torrent')
            if torrent:
                await callback_query.answer("Iniciando descarga...")
                await self._download_torrent(client, callback_query.message, torrent, multiple_files=True)
            else:
                await callback_query.answer("❌ No hay torrent disponible", show_alert=True)
        
        elif data.startswith("nyaa_dl_all_"):
            parts = data.split("_")
            cache_id = parts[3]
            start_idx = int(parts[4])
            end_idx = int(parts[5])
            
            results = search_cache.get(cache_id)
            if not results:
                await callback_query.answer("❌ Resultados no encontrados", show_alert=True)
                return
            
            await callback_query.answer(f"Descargando {end_idx-start_idx} resultados...")
            
            for i in range(start_idx, end_idx):
                result = results[i]
                
                download_link = None
                if result.get('magnet'):
                    download_link = result['magnet']
                elif result.get('torrent'):
                    download_link = result['torrent']
                
                if download_link:
                    temp_msg = await callback_query.message.reply(f"🔄 Descargando #{i+1}: {result['name'][:50]}...")
                    await self._download_torrent(client, temp_msg, download_link, multiple_files=True)
                    await asyncio.sleep(2)
    
    def start_flask(self):
        if self.flask_thread and self.flask_thread.is_alive():
            return
            
        self.flask_thread = threading.Thread(target=run_flask, daemon=True)
        self.flask_thread.start()
    
    def start_subscription_checker(self):
        schedule.every(1).minutes.do(check_subscriptions)
        self.subscription_thread = threading.Thread(target=subscription_worker, daemon=True)
        self.subscription_thread.start()
        print("Subscription checker started")
    
    def run(self):
        self.running = True
        self.start_subscription_checker()
        
        if args.flask:
            self.start_flask()
        
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.create_task(self._process_notifications())
        self.app.run()

def main():
    global args
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
        print("Usa: python bot.py -A API_ID -H API_HASH -T BOT_TOKEN")
        print("O configura variables de entorno: API_ID, API_HASH, BOT_TOKEN")
        sys.exit(1)
    
    os.makedirs("downloads", exist_ok=True)
    
    bot = NekoTelegram(api_id, api_hash, bot_token)
    bot.run()

if __name__ == "__main__":
    main()
