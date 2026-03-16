import os
import sys
import argparse
import threading
import random
import string
import re
import requests
import libtorrent as lt
import bencodepy
import time
import schedule
import asyncio
from flask import Flask
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.enums import ParseMode
import nyaa

app = Flask(__name__)

@app.route("/")
def base_flask():
    return "Bot is running!"

def run_flask():
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)

search_cache = {}
downloads = {}
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

class DownloadHandler:
    def __init__(self):
        self.session = lt.session()
        self.session.listen_on(6881, 6891)
        self.downloads = {}
        
    def add_magnet(self, magnet_link, save_path):
        params = {
            'save_path': save_path,
            'storage_mode': lt.storage_mode_t.storage_mode_sparse,
            'auto_managed': True,
            'duplicate_is_error': True
        }
        
        handle = lt.add_magnet_uri(self.session, magnet_link, params)
        download_id = generate_cache_id()
        
        self.downloads[download_id] = {
            'handle': handle,
            'magnet': magnet_link,
            'path': save_path,
            'start_time': time.time(),
            'status': 'downloading'
        }
        
        return download_id
    
    def add_torrent_file(self, torrent_data, save_path):
        info = lt.torrent_info(torrent_data)
        params = {
            'save_path': save_path,
            'storage_mode': lt.storage_mode_t.storage_mode_sparse,
            'auto_managed': True,
            'ti': info
        }
        
        handle = self.session.add_torrent(params)
        download_id = generate_cache_id()
        
        self.downloads[download_id] = {
            'handle': handle,
            'path': save_path,
            'start_time': time.time(),
            'status': 'downloading',
            'name': info.name()
        }
        
        return download_id
    
    def get_status(self, download_id):
        if download_id not in self.downloads:
            return None
            
        download = self.downloads[download_id]
        handle = download['handle']
        
        if not handle.is_valid():
            download['status'] = 'error'
            return download
            
        status = handle.status()
        
        download['status'] = 'downloading'
        download['progress'] = status.progress * 100
        download['download_rate'] = status.download_rate
        download['upload_rate'] = status.upload_rate
        download['total_download'] = status.total_download
        download['total_upload'] = status.total_upload
        download['num_peers'] = status.num_peers
        download['num_seeds'] = status.num_seeds
        download['state'] = str(status.state)
        download['name'] = handle.torrent_file().name() if handle.has_metadata() else 'Obteniendo metadata...'
        
        if status.is_finished:
            download['status'] = 'finished'
            
        return download
    
    def remove_download(self, download_id):
        if download_id in self.downloads:
            handle = self.downloads[download_id]['handle']
            if handle.is_valid():
                self.session.remove_torrent(handle)
            del self.downloads[download_id]
            return True
        return False

download_handler = DownloadHandler()

def subscription_worker():
    while True:
        schedule.run_pending()
        time.sleep(1)

def check_subscriptions():
    with subscription_lock:
        for sub_id, sub_data in list(subscriptions.items()):
            try:
                chat_id = sub_data['chat_id']
                query = sub_data['query']
                adult = sub_data['adult']
                last_result = sub_data['last_result']
                
                nyaa_search = nyaa.Nyaa_search()
                if adult:
                    results = nyaa_search.nyaafap(query)
                else:
                    results = nyaa_search.nyaafun(query)
                
                if results and len(results) > 0:
                    first_result = results[0]
                    
                    if last_result is None or first_result['date'] != last_result['date']:
                        sub_data['last_result'] = first_result
                        
                        if first_result.get('magnet'):
                            download_link = first_result['magnet']
                        elif first_result.get('torrent'):
                            download_link = first_result['torrent']
                        else:
                            continue
                        
                        text = f"🆕 **Nuevo resultado encontrado para:** `{query}`\n\n"
                        text += f"**{first_result['name']}**\n"
                        text += f"📦 Tamaño: {first_result['size']}\n"
                        text += f"📅 Fecha: {first_result['date']}\n\n"
                        text += "Iniciando descarga automática..."
                        
                        class TempMessage:
                            def __init__(self, chat_id):
                                self.chat = type('obj', (object,), {'id': chat_id})
                                self.text = ""
                        
                        temp_msg = TempMessage(chat_id)
                        
                        bot = None
                        for thread in threading.enumerate():
                            if hasattr(thread, '_target') and thread._target and 'run_bot' in str(thread._target):
                                bot = getattr(thread, 'bot_instance', None)
                                break
                        
                        if bot:
                            asyncio.run_coroutine_threadsafe(
                                bot._send_subscription_notification(chat_id, text, download_link),
                                bot.app.loop
                            )
                            
            except Exception as e:
                print(f"Error en subscription check: {e}")

class NekoTelegram:
    def __init__(self, api_id, api_hash, bot_token):
        self.api_id = api_id
        self.api_hash = api_hash
        self.bot_token = bot_token
        self.app = Client("nekobot", api_id=int(api_id), api_hash=api_hash, bot_token=bot_token)
        self.nyaa = nyaa.Nyaa_search()
        self.flask_thread = None
        self.subscription_thread = None
        
        @self.app.on_message(filters.private)
        async def handle_message(client: Client, message: Message):
            await self._handle_message(client, message)
        
        @self.app.on_callback_query()
        async def handle_callback(client: Client, callback_query: CallbackQuery):
            await self._handle_callback(client, callback_query)
    
    async def _handle_message(self, client: Client, message: Message):
        if not message.text and not message.document:
            return
        
        if message.document:
            await self._handle_torrent_file(client, message)
            return
        
        text = message.text.strip()

        if text.startswith("/start"):
            await message.reply("Bot is running!\n\nComandos disponibles:\n/dl <magnet o URL> - Descargar torrent\n/nyaa <query> - Buscar en Nyaa\n/sub <texto> - Suscribirse a búsqueda\n/sub18 <texto> - Suscribirse a búsqueda +18\n/rmsub <texto> - Eliminar suscripción\n/rmsub18 <texto> - Eliminar suscripción +18\n/status <id> - Ver estado de descarga\n/misubs - Ver suscripciones activas")
        
        elif text.startswith("/dl "):
            arg = text[4:].strip()
            await self._handle_download(client, message, arg)
        
        elif text.startswith("/status "):
            download_id = text[8:].strip()
            await self._check_status(client, message, download_id)
        
        elif text.startswith("/nyaa "):
            query = text[6:].strip()
            await self._search_nyaa(client, message, query, False)
        
        elif text.startswith("/nyaa18 "):
            query = text[8:].strip()
            await self._search_nyaa(client, message, query, True)
        
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
                    'created_at': time.time(),
                    'message_id': status_msg.id
                }
            
            text = f"✅ **Suscripción activada**\n\n"
            text += f"**Término:** `{query}`\n"
            text += f"**Modo:** {'+18' if adult else 'Normal'}\n\n"
            text += f"**Último resultado guardado:**\n"
            text += f"**{first_result['name']}**\n"
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
        for sub_id, sub_data in user_subs:
            text += f"**Término:** `{sub_data['query']}`\n"
            text += f"**Modo:** {'+18' if sub_data['adult'] else 'Normal'}\n"
            text += f"**Último:** {sub_data['last_result']['name'][:50]}...\n"
            text += f"**Fecha:** {sub_data['last_result']['date']}\n"
            text += f"`{sub_id}`\n\n"
        
        await message.reply(text)
    
    async def _send_subscription_notification(self, chat_id, text, download_link):
        try:
            msg = await self.app.send_message(chat_id, text)
            await self._handle_download(self.app, msg, download_link)
        except Exception as e:
            print(f"Error sending notification: {e}")
    
    async def _handle_torrent_file(self, client: Client, message: Message):
        if not message.document:
            return
            
        file_name = message.document.file_name
        if not file_name.endswith('.torrent'):
            await message.reply("❌ El archivo no es un torrent válido")
            return
        
        status_msg = await message.reply("📥 Descargando archivo torrent...")
        
        try:
            file_path = await message.download()
            
            with open(file_path, 'rb') as f:
                torrent_data = f.read()
            
            try:
                bencodepy.decode(torrent_data)
            except:
                os.remove(file_path)
                await status_msg.edit_text("❌ Archivo torrent inválido")
                return
            
            download_id = download_handler.add_torrent_file(torrent_data, "./downloads")
            downloads[download_id] = {
                'chat_id': message.chat.id,
                'message_id': status_msg.id
            }
            
            os.remove(file_path)
            
            await status_msg.edit_text(
                f"✅ Torrent añadido: {file_name}\n"
                f"ID: `{download_id}`\n\n"
                f"Usa /status {download_id} para ver el progreso"
            )
            
        except Exception as e:
            await status_msg.edit_text(f"❌ Error: {str(e)}")
    
    async def _handle_download(self, client: Client, message: Message, arg):
        status_msg = await message.reply("⏳ Procesando...")
        
        try:
            save_path = f"./downloads/{message.chat.id}"
            os.makedirs(save_path, exist_ok=True)
            
            if arg.startswith("magnet:"):
                download_id = download_handler.add_magnet(arg, save_path)
                
                await status_msg.edit_text(
                    f"✅ Magnet añadido\n"
                    f"ID: `{download_id}`\n\n"
                    f"Usa /status {download_id} para ver el progreso"
                )
                
            elif arg.startswith("http://") or arg.startswith("https://"):
                if arg.endswith(".torrent"):
                    await status_msg.edit_text("📥 Descargando archivo torrent...")
                    
                    response = requests.get(arg, timeout=30)
                    if response.status_code == 200:
                        try:
                            bencodepy.decode(response.content)
                        except:
                            await status_msg.edit_text("❌ El archivo descargado no es un torrent válido")
                            return
                        
                        download_id = download_handler.add_torrent_file(response.content, save_path)
                        
                        await status_msg.edit_text(
                            f"✅ Torrent añadido desde URL\n"
                            f"ID: `{download_id}`\n\n"
                            f"Usa /status {download_id} para ver el progreso"
                        )
                    else:
                        await status_msg.edit_text(f"❌ Error al descargar: HTTP {response.status_code}")
                else:
                    await status_msg.edit_text("❌ La URL no parece ser un archivo .torrent")
            else:
                await status_msg.edit_text("❌ Formato no reconocido. Usa un magnet link o URL de archivo .torrent")
                
        except Exception as e:
            await status_msg.edit_text(f"❌ Error: {str(e)}")
    
    async def _check_status(self, client: Client, message: Message, download_id):
        status = download_handler.get_status(download_id)
        
        if not status:
            await message.reply("❌ ID de descarga no encontrado")
            return
        
        if status['status'] == 'finished':
            text = f"✅ **Descarga completada**\n\n"
            text += f"📁 **Archivo:** {status.get('name', 'Desconocido')}\n"
            text += f"📊 **Total descargado:** {format_size(status.get('total_download', 0))}\n"
            text += f"📤 **Total subido:** {format_size(status.get('total_upload', 0))}\n"
            
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🗑️ Eliminar", callback_data=f"dl_remove_{download_id}")]
            ])
            
            await message.reply(text, reply_markup=keyboard)
            
        elif status['status'] == 'downloading':
            progress = status.get('progress', 0)
            download_rate = status.get('download_rate', 0)
            upload_rate = status.get('upload_rate', 0)
            num_peers = status.get('num_peers', 0)
            num_seeds = status.get('num_seeds', 0)
            
            bar_length = 20
            filled = int(bar_length * progress / 100)
            bar = '█' * filled + '░' * (bar_length - filled)
            
            text = f"⬇️ **Descargando...**\n\n"
            text += f"📁 **Archivo:** {status.get('name', 'Desconocido')}\n"
            text += f"📊 **Progreso:** {progress:.2f}%\n"
            text += f"`{bar}`\n\n"
            text += f"⬇️ **Descarga:** {format_size(download_rate)}/s\n"
            text += f"⬆️ **Subida:** {format_size(upload_rate)}/s\n"
            text += f"👥 **Peers:** {num_peers} | 🌱 **Seeds:** {num_seeds}\n"
            text += f"📥 **Total:** {format_size(status.get('total_download', 0))}\n"
            text += f"📤 **Subido:** {format_size(status.get('total_upload', 0))}\n"
            text += f"🔄 **Estado:** {status.get('state', 'Desconocido')}\n\n"
            text += f"ID: `{download_id}`"
            
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Actualizar", callback_data=f"dl_status_{download_id}"),
                 InlineKeyboardButton("🗑️ Eliminar", callback_data=f"dl_remove_{download_id}")]
            ])
            
            msg = await message.reply(text, reply_markup=keyboard)
            
            if download_id not in downloads:
                downloads[download_id] = {}
            downloads[download_id]['last_status_msg'] = msg.id
        else:
            await message.reply("❌ Estado desconocido")
    
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
            search_cache[cache_id] = results[:10]
            
            text = f"**Resultados para:** `{query}`\n\n"
            for i, result in enumerate(results[:5], 1):
                text += f"{i}. **{result['name'][:50]}**...\n"
                text += f"📦 {result['size']} | 📅 {result['date']}\n\n"
            
            keyboard = []
            for i, result in enumerate(results[:10], 1):
                keyboard.append([InlineKeyboardButton(f"{i}. {result['name'][:30]}...", callback_data=f"nyaa_detail_{cache_id}_{i-1}")])
            
            if len(results) > 5:
                keyboard.append([
                    InlineKeyboardButton("⬅️ Anterior", callback_data=f"nyaa_page_{cache_id}_prev"),
                    InlineKeyboardButton(f"1/{max(1, (len(results)-1)//5 + 1)}", callback_data="noop"),
                    InlineKeyboardButton("Siguiente ➡️", callback_data=f"nyaa_page_{cache_id}_next")
                ])
            
            await status_msg.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
            
        except Exception as e:
            await status_msg.edit_text(f"❌ Error: {str(e)}")
    
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
                    await callback_query.message.edit_text("✅ Suscripción cancelada")
                    await callback_query.answer()
                else:
                    await callback_query.answer("Suscripción no encontrada", show_alert=True)
            return
        
        if data.startswith("dl_"):
            parts = data.split("_")
            
            if parts[1] == "status":
                download_id = parts[2]
                await self._check_status(client, callback_query.message, download_id)
                await callback_query.answer()
                
            elif parts[1] == "remove":
                download_id = parts[2]
                if download_handler.remove_download(download_id):
                    await callback_query.message.edit_text("✅ Descarga eliminada")
                else:
                    await callback_query.answer("Error al eliminar", show_alert=True)
                await callback_query.answer()
                
            return
        
        if data.startswith("nyaa_"):
            parts = data.split("_")
            
            if parts[1] == "page":
                cache_id = parts[2]
                action = parts[3]
                
                if cache_id not in search_cache:
                    await callback_query.answer("Búsqueda expirada")
                    return
                
                results = search_cache[cache_id]
                current_text = callback_query.message.text
                current_page = 1
                
                if "Página" in current_text:
                    try:
                        current_page = int(current_text.split("Página")[1].split("/")[0].strip())
                    except:
                        current_page = 1
                
                if action == "next":
                    current_page += 1
                elif action == "prev":
                    current_page -= 1
                
                total_pages = max(1, (len(results)-1)//5 + 1)
                current_page = max(1, min(current_page, total_pages))
                
                start_idx = (current_page - 1) * 5
                end_idx = min(start_idx + 5, len(results))
                
                text = f"**Resultados (Página {current_page}/{total_pages})**\n\n"
                for i, result in enumerate(results[start_idx:end_idx], start_idx + 1):
                    text += f"{i}. **{result['name'][:50]}**...\n"
                    text += f"📦 {result['size']} | 📅 {result['date']}\n\n"
                
                keyboard = []
                for i in range(start_idx, end_idx):
                    idx = i
                    keyboard.append([InlineKeyboardButton(f"{i+1}. {results[i]['name'][:30]}...", callback_data=f"nyaa_detail_{cache_id}_{i}")])
                
                nav_buttons = []
                if current_page > 1:
                    nav_buttons.append(InlineKeyboardButton("⬅️ Anterior", callback_data=f"nyaa_page_{cache_id}_prev"))
                else:
                    nav_buttons.append(InlineKeyboardButton("⬅️ Anterior", callback_data="noop"))
                
                nav_buttons.append(InlineKeyboardButton(f"{current_page}/{total_pages}", callback_data="noop"))
                
                if current_page < total_pages:
                    nav_buttons.append(InlineKeyboardButton("Siguiente ➡️", callback_data=f"nyaa_page_{cache_id}_next"))
                else:
                    nav_buttons.append(InlineKeyboardButton("Siguiente ➡️", callback_data="noop"))
                
                keyboard.append(nav_buttons)
                
                await callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
                await callback_query.answer()
            
            elif parts[1] == "detail":
                cache_id = parts[2]
                result_idx = int(parts[3])
                
                if cache_id not in search_cache:
                    await callback_query.answer("Búsqueda expirada")
                    return
                
                result = search_cache[cache_id][result_idx]
                
                text = f"**{result['name']}**\n\n"
                text += f"📦 Tamaño: {result['size']}\n"
                text += f"📅 Fecha: {result['date']}\n\n"
                text += "Selecciona una opción:"
                
                keyboard = [
                    [InlineKeyboardButton("🧲 Descargar Magnet", callback_data=f"nyaa_dl_magnet_{cache_id}_{result_idx}")],
                    [InlineKeyboardButton("⬇️ Descargar Torrent", callback_data=f"nyaa_dl_torrent_{cache_id}_{result_idx}")],
                    [InlineKeyboardButton("🔙 Volver", callback_data=f"nyaa_page_{cache_id}_1")]
                ]
                
                await callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
                await callback_query.answer()
            
            elif parts[1] == "dl":
                if parts[2] == "magnet":
                    cache_id = parts[3]
                    result_idx = int(parts[4])
                    
                    if cache_id not in search_cache:
                        await callback_query.answer("Búsqueda expirada")
                        return
                    
                    result = search_cache[cache_id][result_idx]
                    
                    if result['magnet']:
                        await callback_query.answer("Iniciando descarga...")
                        await self._handle_download(client, callback_query.message, result['magnet'])
                    else:
                        await callback_query.answer("No hay magnet disponible", show_alert=True)
                
                elif parts[2] == "torrent":
                    cache_id = parts[3]
                    result_idx = int(parts[4])
                    
                    if cache_id not in search_cache:
                        await callback_query.answer("Búsqueda expirada")
                        return
                    
                    result = search_cache[cache_id][result_idx]
                    
                    if result['torrent']:
                        await callback_query.answer("Iniciando descarga...")
                        await self._handle_download(client, callback_query.message, result['torrent'])
                    else:
                        await callback_query.answer("No hay torrent disponible", show_alert=True)
            
            else:
                cache_id = parts[1]
                result_idx = int(parts[2])
                
                if cache_id not in search_cache:
                    await callback_query.answer("Búsqueda expirada")
                    return
                
                result = search_cache[cache_id][result_idx]
                
                text = f"**{result['name']}**\n\n"
                text += f"📦 Tamaño: {result['size']}\n"
                text += f"📅 Fecha: {result['date']}\n\n"
                text += "Selecciona una opción:"
                
                keyboard = [
                    [InlineKeyboardButton("🧲 Descargar Magnet", callback_data=f"nyaa_dl_magnet_{cache_id}_{result_idx}")],
                    [InlineKeyboardButton("⬇️ Descargar Torrent", callback_data=f"nyaa_dl_torrent_{cache_id}_{result_idx}")],
                    [InlineKeyboardButton("🔙 Volver", callback_data=f"nyaa_page_{cache_id}_1")]
                ]
                
                await callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
                await callback_query.answer()
    
    def start_flask(self):
        if self.flask_thread and self.flask_thread.is_alive():
            return
            
        self.flask_thread = threading.Thread(target=run_flask, daemon=True)
        self.flask_thread.start()
    
    def start_subscription_checker(self):
        schedule.every(1).minutes.do(check_subscriptions)
        self.subscription_thread = threading.Thread(target=subscription_worker, daemon=True)
        self.subscription_thread.start()
    
    def run(self):
        self.start_subscription_checker()
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
    
    os.makedirs("./downloads", exist_ok=True)
    
    bot = NekoTelegram(api_id, api_hash, bot_token)

    if args.flask:
        bot.start_flask()
    
    bot.run()

if __name__ == "__main__":
    main()
