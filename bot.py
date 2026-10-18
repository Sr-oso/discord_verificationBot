import os
import time
import re
import asyncio
import sqlite3
import logging
import copy
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

# CONFIGURACIÓN

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
PLAZO_VERIFICACION = 24 * 60 * 60
BASE_DATOS = Path(__file__).with_name("verificaciones.db")
NOMBRE_ROL_PENDIENTE = "No verificado"
NOMBRE_ROL_VERIFICADO = "Verificado"
NOMBRE_CANAL_VERIFICADOR = "canal_verificador"
INTERVALO_REVISION = 60

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s:%(name)s:%(message)s",
)
logger = logging.getLogger("verificador")

intents = discord.Intents.default()
intents.members = True  # También debe estar activado en Developer Portal.

bot = commands.Bot(command_prefix="!", intents=intents)
vista_registrada = False
comandos_sincronizados = set()
sincronizaciones_activas = set()
locks_verificacion = {}

# BASE DE DATOS

def conectar_db():
    conn = sqlite3.connect(BASE_DATOS, timeout=15)
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def inicializar_db():
    with conectar_db() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pendientes (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                ingreso REAL NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            )
        """)
        # Registro persistente de avisos para evitar volver a enviarlos al reiniciar.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS avisos (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                enviado REAL NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            )
        """)
        conn.commit()


def registrar_pendiente(guild_id, user_id, ingreso=None):
    if ingreso is None:
        ingreso = time.time()
    with conectar_db() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO pendientes (guild_id, user_id, ingreso)
            VALUES (?, ?, ?)
        """, (guild_id, user_id, ingreso))
        conn.commit()


def obtener_pendiente(guild_id, user_id):
    with conectar_db() as conn:
        return conn.execute("""
            SELECT ingreso FROM pendientes WHERE guild_id = ? AND user_id = ?
        """, (guild_id, user_id)).fetchone()


def quitar_pendiente(guild_id, user_id):
    with conectar_db() as conn:
        conn.execute("DELETE FROM pendientes WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        conn.commit()


def obtener_pendientes():
    with conectar_db() as conn:
        return conn.execute("SELECT guild_id, user_id, ingreso FROM pendientes").fetchall()


def registrar_aviso(guild_id, user_id, message_id, enviado=None):
    if enviado is None:
        enviado = time.time()
    with conectar_db() as conn:
        conn.execute("""
            INSERT INTO avisos (guild_id, user_id, message_id, enviado)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                message_id = excluded.message_id,
                enviado = excluded.enviado
        """, (guild_id, user_id, message_id, enviado))
        conn.commit()


def obtener_avisos_db(guild_id):
    with conectar_db() as conn:
        return {
            user_id: (enviado, message_id)
            for user_id, enviado, message_id in conn.execute(
                "SELECT user_id, enviado, message_id FROM avisos WHERE guild_id = ?",
                (guild_id,),
            ).fetchall()
        }


def obtener_estado_db(guild_id=None):
    ahora = time.time()
    with conectar_db() as conn:
        if guild_id is None:
            filas = conn.execute("SELECT ingreso FROM pendientes").fetchall()
        else:
            filas = conn.execute("SELECT ingreso FROM pendientes WHERE guild_id = ?", (guild_id,)).fetchall()
    total = len(filas)
    vencidos = sum(1 for (ingreso,) in filas if ahora - ingreso >= PLAZO_VERIFICACION)
    return total, vencidos

# BÚSQUEDA DE ROLES Y CANALES

def buscar_rol(guild, nombre):
    return discord.utils.get(guild.roles, name=nombre)


def buscar_canal(guild):
    return discord.utils.get(guild.text_channels, name=NOMBRE_CANAL_VERIFICADOR)


def miembro_mencionado_en_mensaje(mensaje, user_id):
    if any(usuario.id == user_id for usuario in mensaje.mentions):
        return True
    patron = rf"<@!?{user_id}>"
    textos = [mensaje.content or ""]
    for embed in mensaje.embeds:
        textos.extend([embed.title or "", embed.description or ""])
        textos.extend(campo.value or "" for campo in embed.fields)
        if embed.footer and embed.footer.text:
            textos.append(embed.footer.text)
    return any(re.search(patron, texto) for texto in textos)


# PERMISOS

async def establecer_permiso(canal, objetivo, **cambios):
    actual = canal.overwrites_for(objetivo)
    nuevo = copy.copy(actual)
    for permiso, valor in cambios.items():
        setattr(nuevo, permiso, valor)
    if nuevo != actual:
        await canal.set_permissions(
            objetivo,
            overwrite=nuevo,
            reason="Configuración del sistema de verificación",
        )


async def configurar_permisos(guild):
    """Aplica denegaciones explícitas al rol pendiente en categorías y canales."""
    pendiente = buscar_rol(guild, NOMBRE_ROL_PENDIENTE)
    verificado = buscar_rol(guild, NOMBRE_ROL_VERIFICADO)
    canal_verificador = buscar_canal(guild)

    if pendiente is None or verificado is None or canal_verificador is None:
        logger.error(
            "Configuración incompleta en %s (No verificado=%s, Verificado=%s, canal=%s)",
            guild.name, bool(pendiente), bool(verificado), bool(canal_verificador),
        )
        return False

    errores = []
    # El canal de verificación se reserva a los pendientes y al bot.
    try:
        await establecer_permiso(canal_verificador, guild.default_role, view_channel=False)
        await establecer_permiso(
            canal_verificador, pendiente,
            view_channel=True, read_message_history=True,
            send_messages=False, send_messages_in_threads=False,
            create_public_threads=False, create_private_threads=False,
            add_reactions=False, connect=False, speak=False, stream=False,
        )
        await establecer_permiso(canal_verificador, verificado, view_channel=False)
        if guild.me:
            await establecer_permiso(
                canal_verificador, guild.me,
                view_channel=True, read_message_history=True,
                send_messages=True, embed_links=True,
            )
    except (discord.Forbidden, discord.HTTPException) as exc:
        errores.append(f"{canal_verificador.name}: {type(exc).__name__}")
        logger.exception("No pude configurar el canal de verificación en %s", guild.name)

    # Denegación explícita en todas las categorías y canales distintos del verificador.
    cambios_restringidos = {
        "view_channel": False,
        "send_messages": False,
        "send_messages_in_threads": False,
        "create_public_threads": False,
        "create_private_threads": False,
        "connect": False,
        "speak": False,
        "stream": False,
        "use_application_commands": False,
    }
    for canal in guild.channels:
        if canal.id == canal_verificador.id:
            continue
        try:
            await establecer_permiso(canal, pendiente, **cambios_restringidos)
        except (discord.Forbidden, discord.HTTPException) as exc:
            errores.append(f"{canal.name}: {type(exc).__name__}")
            logger.exception("No pude restringir %s en %s", canal.name, guild.name)

    if errores:
        logger.warning("Permisos aplicados parcialmente en %s; errores: %s", guild.name, ", ".join(errores))
        return False

    logger.info("Permisos revisados en %s", guild.name)
    return True

# HISTORIAL Y MENSAJES DE BIENVENIDA

async def obtener_usuarios_avisados(canal):
    """Devuelve {user_id: (timestamp, message_id)}. None significa que falló la lectura."""
    avisados = obtener_avisos_db(canal.guild.id)
    try:
        async for mensaje in canal.history(limit=2000, oldest_first=True):
            if bot.user is None or mensaje.author.id != bot.user.id:
                continue
            ids = {usuario.id for usuario in mensaje.mentions}
            for embed in mensaje.embeds:
                textos = [embed.title or "", embed.description or ""]
                textos.extend(campo.value or "" for campo in embed.fields)
                if embed.footer and embed.footer.text:
                    textos.append(embed.footer.text)
                for texto in textos:
                    ids.update(int(x) for x in re.findall(r"<@!?(\d+)>", texto))
            for user_id in ids:
                fecha = mensaje.created_at.timestamp()
                if user_id not in avisados or fecha < avisados[user_id][0]:
                    avisados[user_id] = (fecha, mensaje.id)
                    # Migrar avisos antiguos a la base de datos.
                    registrar_aviso(canal.guild.id, user_id, mensaje.id, fecha)
    except discord.Forbidden:
        logger.error("No puedo leer el historial de %s; cancelo sincronización para evitar duplicados", canal.name)
        return None
    except discord.HTTPException:
        logger.exception("Error leyendo historial de %s; cancelo sincronización", canal.name)
        return None
    except sqlite3.Error:
        logger.exception("Error leyendo avisos guardados para %s", canal.guild.name)
        return None
    return avisados


async def enviar_bienvenida(miembro):
    canal = buscar_canal(miembro.guild)
    if canal is None:
        logger.error("No existe el canal %s en %s", NOMBRE_CANAL_VERIFICADOR, miembro.guild.name)
        return False

    embed = discord.Embed(
        title="Verificación del servidor",
        description=(
            f"Hola {miembro.mention}, para acceder al resto del servidor debes aceptar las condiciones y restricciones.\n\n"
            "Pulsa el botón verde que aparece debajo de este mensaje. Tienes 24 horas desde el aviso inicial para completar la verificación."
        ),
        color=discord.Color.green(),
    )
    embed.set_footer(text="Sistema automático de verificación")

    try:
        mensaje = await canal.send(
            content=miembro.mention,
            embed=embed,
            view=BotonVerificacion(),
            allowed_mentions=discord.AllowedMentions(users=True),
        )
        registrar_aviso(miembro.guild.id, miembro.id, mensaje.id, mensaje.created_at.timestamp())
        logger.info("Mensaje de verificación enviado a %s (%s)", miembro, miembro.id)
        return True
    except discord.Forbidden:
        logger.error("No tengo permisos para enviar mensajes/embeds en %s", canal.name)
    except (discord.HTTPException, sqlite3.Error):
        logger.exception("No se pudo completar el envío de bienvenida a %s", miembro)
    return False

# BOTÓN DE VERIFICACIÓN

class BotonVerificacion(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Acepto las condiciones y restricciones del servidor",
        style=discord.ButtonStyle.success,
        custom_id="verificacion:aceptar",
    )
    async def aceptar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Este botón solo funciona dentro del servidor.", ephemeral=True)
            return

        miembro = interaction.user
        guild = interaction.guild
        if miembro.bot:
            await interaction.response.send_message("Las cuentas de bot no necesitan verificarse.", ephemeral=True)
            return

        lock = locks_verificacion.setdefault((guild.id, miembro.id), asyncio.Lock())
        async with lock:
            pendiente = buscar_rol(guild, NOMBRE_ROL_PENDIENTE)
            verificado = buscar_rol(guild, NOMBRE_ROL_VERIFICADO)
            canal_verificador = buscar_canal(guild)

            if pendiente is None or verificado is None or canal_verificador is None:
                await interaction.response.send_message("La verificación no está configurada correctamente. Contacta a un administrador.", ephemeral=True)
                return
            if verificado in miembro.roles:
                await interaction.response.send_message("Ya estás verificado. No necesitas pulsar el botón otra vez.", ephemeral=True)
                return
            if pendiente not in miembro.roles:
                await interaction.response.send_message("No tienes una verificación pendiente.", ephemeral=True)
                return

            registro = obtener_pendiente(guild.id, miembro.id)
            if registro is None:
                # Recuperación segura: solo aceptar el mensaje de bienvenida personal del bot.
                mensaje = interaction.message
                mensaje_valido = (
                    mensaje is not None
                    and bot.user is not None
                    and mensaje.author.id == bot.user.id
                    and mensaje.channel.id == canal_verificador.id
                    and miembro_mencionado_en_mensaje(mensaje, miembro.id)
                )
                if not mensaje_valido:
                    await interaction.response.send_message(
                        "No encuentro tu registro de verificación. No se reinició tu plazo; contacta a un administrador.",
                        ephemeral=True,
                    )
                    logger.warning("Sin registro válido al pulsar botón: guild=%s user=%s", guild.id, miembro.id)
                    return
                registrar_pendiente(guild.id, miembro.id, mensaje.created_at.timestamp())
                registro = obtener_pendiente(guild.id, miembro.id)

            ingreso = registro[0]
            if time.time() - ingreso >= PLAZO_VERIFICACION:
                await interaction.response.send_message(
                    "Tu plazo de verificación ha vencido. Contacta a un administrador si necesitas ayuda.",
                    ephemeral=True,
                )
                return

            bot_member = guild.me
            if bot_member is None or bot_member.top_role <= max(pendiente, verificado, key=lambda rol: rol.position):
                await interaction.response.send_message(
                    "No puedo administrar tus roles por la jerarquía del servidor. Un administrador debe subir mi rol por encima de los roles de verificación.",
                    ephemeral=True,
                )
                logger.error("Jerarquía de roles insuficiente en %s", guild.name)
                return

            try:
                await miembro.add_roles(verificado, reason="Verificación aceptada por el miembro")
                try:
                    await miembro.remove_roles(pendiente, reason="Verificación completada")
                except (discord.Forbidden, discord.HTTPException):
                    # Intentar revertir para no dejar roles en estado parcial.
                    logger.exception("No pude retirar No verificado a %s; intento revertir Verificado", miembro)
                    try:
                        await miembro.remove_roles(verificado, reason="Reversión de verificación incompleta")
                    except (discord.Forbidden, discord.HTTPException):
                        logger.critical("No pude revertir roles de %s; revisión manual necesaria", miembro)
                    await interaction.response.send_message(
                        "No pude completar el cambio de roles. No cierres el servidor; avisa a un administrador para revisar tus roles.",
                        ephemeral=True,
                    )
                    return

                quitar_pendiente(guild.id, miembro.id)
                await interaction.response.send_message("¡Verificación completada! Ya tienes acceso al servidor.", ephemeral=True)
                logger.info("VERIFICADO guild=%s user=%s (%s)", guild.id, miembro.id, miembro)
            except discord.Forbidden:
                await interaction.response.send_message("No tengo permisos para cambiar tus roles. Contacta a un administrador.", ephemeral=True)
                logger.exception("Permiso denegado al verificar a %s", miembro)
            except (discord.HTTPException, sqlite3.Error):
                logger.exception("Error al verificar a %s", miembro)
                if not interaction.response.is_done():
                    await interaction.response.send_message("Ocurrió un error al verificarte. Inténtalo más tarde.", ephemeral=True)


# SINCRONIZACIÓN DE MIEMBROS EXISTENTES

async def sincronizar_miembros_existentes(guild):
    if guild.id in sincronizaciones_activas:
        logger.info("Ya hay una sincronización en curso para %s", guild.name)
        return
    sincronizaciones_activas.add(guild.id)
    try:
        logger.info("Iniciando sincronización en %s", guild.name)
        pendiente = buscar_rol(guild, NOMBRE_ROL_PENDIENTE)
        verificado = buscar_rol(guild, NOMBRE_ROL_VERIFICADO)
        canal = buscar_canal(guild)
        if pendiente is None or verificado is None or canal is None:
            logger.error("Sincronización cancelada: falta un rol o el canal en %s", guild.name)
            return

        try:
            miembros = [m async for m in guild.fetch_members(limit=None)]
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("No pude descargar todos los miembros de %s", guild.name)
            return
        logger.info("Integrantes descargados: %s", len(miembros))

        avisados = await obtener_usuarios_avisados(canal)
        if avisados is None:
            logger.error("Sincronización cancelada para evitar mensajes duplicados en %s", guild.name)
            return
        logger.info("Integrantes identificados en avisos: %s", len(avisados))

        cantidad_pendientes = 0
        enviados = 0
        reparados = 0
        for miembro in miembros:
            if miembro.bot:
                continue
            tiene_pendiente = pendiente in miembro.roles
            tiene_verificado = verificado in miembro.roles

            if tiene_pendiente and tiene_verificado:
                try:
                    await miembro.remove_roles(pendiente, reason="Reparación: miembro ya verificado")
                    quitar_pendiente(guild.id, miembro.id)
                    reparados += 1
                    logger.warning("Reparado estado de roles para %s", miembro)
                except (discord.Forbidden, discord.HTTPException):
                    logger.exception("No pude reparar los roles de %s", miembro)
                continue

            if not tiene_pendiente:
                continue

            cantidad_pendientes += 1
            registro = obtener_pendiente(guild.id, miembro.id)
            aviso = avisados.get(miembro.id)
            if registro is None:
                if aviso is not None:
                    registrar_pendiente(guild.id, miembro.id, aviso[0])
                else:
                    registrar_pendiente(guild.id, miembro.id)

            # Si no hay aviso conocido, publicar uno nuevo; no contar como enviado si falla.
            if aviso is None:
                if await enviar_bienvenida(miembro):
                    enviados += 1
                    await asyncio.sleep(1)

        logger.info(
            "Sincronización terminada en %s: miembros=%d, pendientes=%d, avisos nuevos=%d, roles reparados=%d",
            guild.name, len(miembros), cantidad_pendientes, enviados, reparados,
        )
    except (sqlite3.Error, discord.HTTPException):
        logger.exception("Error durante la sincronización de %s", guild.name)
    finally:
        sincronizaciones_activas.discard(guild.id)

# NUEVOS MIEMBROS Y SALIDAS

@bot.event
async def on_member_join(miembro):
    if miembro.bot:
        return
    pendiente = buscar_rol(miembro.guild, NOMBRE_ROL_PENDIENTE)
    verificado = buscar_rol(miembro.guild, NOMBRE_ROL_VERIFICADO)
    if pendiente is None or verificado is None:
        logger.error("No existen ambos roles de verificación en %s", miembro.guild.name)
        return
    try:
        # Un usuario que reingresa debe pasar de nuevo por la verificación.
        if verificado in miembro.roles:
            await miembro.remove_roles(verificado, reason="Reingreso: reiniciar verificación")
        await miembro.add_roles(pendiente, reason="Nuevo miembro pendiente de verificación")
        registrar_pendiente(miembro.guild.id, miembro.id)
        await enviar_bienvenida(miembro)
    except discord.Forbidden:
        logger.exception("No tengo permisos para preparar la verificación de %s", miembro)
    except (discord.HTTPException, sqlite3.Error):
        logger.exception("Error procesando la entrada de %s", miembro)


@bot.event
async def on_member_remove(miembro):
    quitar_pendiente(miembro.guild.id, miembro.id)
    # Se conserva el registro del aviso para que un reingreso genere un aviso nuevo
    # solo después de que se actualice el registro en on_member_join.
    with conectar_db() as conn:
        conn.execute("DELETE FROM avisos WHERE guild_id = ? AND user_id = ?", (miembro.guild.id, miembro.id))
        conn.commit()

# REVISIÓN DEL PLAZO Y EXPULSIÓN

@tasks.loop(seconds=INTERVALO_REVISION)
async def revisar_pendientes():
    ahora = time.time()
    try:
        filas = obtener_pendientes()
    except sqlite3.Error:
        logger.exception("No pude leer los pendientes de SQLite")
        return

    for guild_id, user_id, ingreso in filas:
        guild = bot.get_guild(guild_id)
        if guild is None:
            continue
        try:
            miembro = guild.get_member(user_id)
            if miembro is None:
                try:
                    miembro = await guild.fetch_member(user_id)
                except discord.NotFound:
                    quitar_pendiente(guild_id, user_id)
                    continue
            if miembro.bot:
                quitar_pendiente(guild_id, user_id)
                continue

            pendiente = buscar_rol(guild, NOMBRE_ROL_PENDIENTE)
            verificado = buscar_rol(guild, NOMBRE_ROL_VERIFICADO)
            if verificado and verificado in miembro.roles:
                if pendiente and pendiente in miembro.roles:
                    try:
                        await miembro.remove_roles(pendiente, reason="Reparación automática: miembro ya verificado")
                    except (discord.Forbidden, discord.HTTPException):
                        logger.exception("No pude retirar el rol pendiente a %s", miembro)
                quitar_pendiente(guild_id, user_id)
                continue
            if pendiente is None or pendiente not in miembro.roles:
                quitar_pendiente(guild_id, user_id)
                continue
            if ahora - ingreso < PLAZO_VERIFICACION:
                continue

            try:
                await miembro.kick(reason="No completó la verificación dentro de las 24 horas")
                quitar_pendiente(guild_id, user_id)
                with conectar_db() as conn:
                    conn.execute("DELETE FROM avisos WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
                    conn.commit()
                logger.info("EXPULSADO_POR_VENCIMIENTO guild=%s user=%s (%s)", guild_id, user_id, miembro)
            except discord.Forbidden:
                logger.error("No tengo permiso/jerarquía para expulsar a %s en %s", miembro, guild.name)
            except discord.HTTPException:
                logger.exception("Error al expulsar a %s", miembro)
        except (discord.Forbidden, discord.HTTPException, sqlite3.Error):
            logger.exception("Error revisando pendiente user=%s guild=%s", user_id, guild_id)


@revisar_pendientes.before_loop
async def antes_de_revisar():
    await bot.wait_until_ready()

# COMANDOS SLASH ADMINISTRATIVOS

def usuario_es_admin(interaction: discord.Interaction):
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        return False
    return interaction.user.guild_permissions.administrator or interaction.user.guild_permissions.manage_guild


async def exigir_admin(interaction: discord.Interaction):
    if not usuario_es_admin(interaction):
        await interaction.response.send_message("Este comando requiere Administrador o Gestionar servidor.", ephemeral=True)
        return False
    return True


@bot.tree.command(name="diagnostico", description="Revisa la configuración del sistema de verificación")
@app_commands.guild_only()
async def diagnostico(interaction: discord.Interaction):
    if not await exigir_admin(interaction):
        return
    guild = interaction.guild
    assert guild is not None
    pendiente = buscar_rol(guild, NOMBRE_ROL_PENDIENTE)
    verificado = buscar_rol(guild, NOMBRE_ROL_VERIFICADO)
    canal = buscar_canal(guild)
    bot_member = guild.me
    checks = []

    checks.append(("Rol No verificado", "Correcto" if pendiente else "FALTA"))
    checks.append(("Rol Verificado", "Correcto" if verificado else "FALTA"))
    checks.append(("Canal de verificación", "Correcto" if canal else "FALTA"))
    checks.append(("Server Members Intent en el código", "Activado" if bot.intents.members else "Desactivado"))

    if bot_member:
        permisos_bot = bot_member.guild_permissions
        checks.append(("Administrar roles", "Sí" if permisos_bot.manage_roles else "NO"))
        checks.append(("Expulsar miembros", "Sí" if permisos_bot.kick_members else "NO"))
        checks.append(("Administrar canales", "Sí" if permisos_bot.manage_channels else "NO"))
        if pendiente and verificado:
            checks.append(("Jerarquía de roles", "Correcta" if bot_member.top_role > max(pendiente, verificado, key=lambda r: r.position) else "NO: sube el rol del bot"))
    if canal and bot_member:
        p = canal.permissions_for(bot_member)
        checks.append(("Enviar mensajes en verificador", "Sí" if p.send_messages else "NO"))
        checks.append(("Insertar enlaces/embeds", "Sí" if p.embed_links else "NO"))
        checks.append(("Leer historial", "Sí" if p.read_message_history else "NO"))
        if pendiente:
            ov = canal.overwrites_for(pendiente)
            checks.append(("Pendientes ven canal verificador", "Sí" if ov.view_channel is True else "REVISAR"))
            checks.append(("Pendientes no escriben", "Sí" if ov.send_messages is False else "REVISAR"))

    # Alertar de roles adicionales que conceden acceso explícito a canales normales.
    conflictos = []
    if pendiente:
        for ch in guild.channels:
            if canal and ch.id == canal.id:
                continue
            for rol, overwrite in ch.overwrites.items():
                if isinstance(rol, discord.Role) and rol != pendiente and overwrite.view_channel is True:
                    conflictos.append(f"{ch.name}: rol '{rol.name}' permite ver canal")
                    break
    if conflictos:
        checks.append(("Posibles permisos que contradicen el bloqueo", f"{len(conflictos)} canal(es); revisa roles adicionales"))
    else:
        checks.append(("Conflictos de visibilidad detectables", "No se encontraron permisos de rol explícitos que permitan ver canales normales"))

    embed = discord.Embed(title="Diagnóstico de verificación", color=discord.Color.blurple())
    for nombre, resultado in checks[:25]:
        embed.add_field(name=nombre, value=resultado, inline=False)
    if conflictos:
        embed.add_field(name="Primeros conflictos", value="\n".join(conflictos[:8]), inline=False)
    embed.set_footer(text="El propietario y los miembros con Administrador pueden omitir las restricciones de canales de Discord.")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="estado", description="Muestra el estado de las verificaciones pendientes")
@app_commands.guild_only()
async def estado(interaction: discord.Interaction):
    if not await exigir_admin(interaction):
        return
    try:
        total, vencidos = obtener_estado_db(interaction.guild.id)
    except sqlite3.Error:
        logger.exception("Error consultando estado de pendientes")
        await interaction.response.send_message("No pude leer la base de datos. Revisa la consola.", ephemeral=True)
        return
    embed = discord.Embed(title="Estado de verificación", color=discord.Color.blurple())
    embed.add_field(name="Pendientes registrados", value=str(total), inline=True)
    embed.add_field(name="Con plazo vencido", value=str(vencidos), inline=True)
    embed.add_field(name="Plazo configurado", value="24 horas", inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="sincronizar", description="Busca miembros con No verificado y corrige avisos pendientes")
@app_commands.guild_only()
async def sincronizar(interaction: discord.Interaction):
    if not await exigir_admin(interaction):
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    await sincronizar_miembros_existentes(interaction.guild)
    await interaction.followup.send("La sincronización terminó o se canceló por un error. Consulta la consola para ver el resultado detallado.", ephemeral=True)


@bot.tree.command(name="configurar_verificacion", description="Vuelve a aplicar los permisos del sistema de verificación")
@app_commands.guild_only()
async def configurar_verificacion(interaction: discord.Interaction):
    if not await exigir_admin(interaction):
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        ok = await configurar_permisos(interaction.guild)
        if ok:
            await interaction.followup.send("Permisos aplicados. Ejecuta /diagnostico y prueba con una cuenta sin Administrador.", ephemeral=True)
        else:
            await interaction.followup.send("La configuración quedó incompleta. Revisa la consola y ejecuta /diagnostico.", ephemeral=True)
    except (discord.Forbidden, discord.HTTPException):
        logger.exception("Error al configurar permisos desde comando")
        await interaction.followup.send("Discord rechazó uno o más cambios de permisos. Revisa jerarquía y permisos del bot.", ephemeral=True)

# EVENTOS DEL BOT

@bot.event
async def on_ready():
    global vista_registrada, comandos_sincronizados
    if not vista_registrada:
        bot.add_view(BotonVerificacion())
        vista_registrada = True
    if not revisar_pendientes.is_running():
        revisar_pendientes.start()

    logger.info("Bot conectado como %s", bot.user)
    for guild in bot.guilds:
        if guild.id not in comandos_sincronizados:
            try:
                # Copiarlos al servidor hace que estén disponibles de inmediato,
                # sin esperar la propagación de comandos globales.
                objeto_guild = discord.Object(id=guild.id)
                bot.tree.copy_global_to(guild=objeto_guild)
                synced = await bot.tree.sync(guild=objeto_guild)
                logger.info("Comandos slash sincronizados en %s: %d", guild.name, len(synced))
            except discord.HTTPException:
                logger.exception("No pude sincronizar comandos slash en %s", guild.name)
            else:
                comandos_sincronizados.add(guild.id)

        try:
            await configurar_permisos(guild)
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("Falta acceso al configurar permisos de %s", guild.name)
        # Sincronización independiente: un error de permisos no debe impedirla.
        await sincronizar_miembros_existentes(guild)


@bot.event
async def on_guild_channel_create(canal):
    try:
        await configurar_permisos(canal.guild)
    except (discord.Forbidden, discord.HTTPException):
        logger.exception("No pude actualizar permisos tras crear %s", canal.name)


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    logger.error("Error de comando prefijo: %s", error)

# INICIO

if __name__ == "__main__":
    inicializar_db()
    if not TOKEN:
        raise RuntimeError("No se encontró DISCORD_TOKEN en el archivo .env")
    bot.run(TOKEN)