# DiscordVerificationBot

Bot de discord desarrollado con python para gestionar la verificación de miembros de un servidor, el bot asigna roles automáticamente, envía un mensaje de bienvenida con un botón de verificación y expulsa a los miembros que no completen el proceso dentro del plazo establecido.

## Características

- Asigna el rol `No verificado` cuando una persona entra al servidor.
- Envía un mensaje de bienvenida con un botón para aceptar las condiciones.
- Al verificar, asigna el rol `Verificado` y elimina el rol `No verificado`.
- Expulsa a los miembros que no se verifican dentro de las 24 horas establecidas.
- Guarda los plazos de verificación en una base de datos SQLite.
- Recupera y sincroniza miembros existentes que todavía necesitan verificarse.
- Incluye comandos slash para consultar el estado, diagnosticar permisos, sincronizar miembros y configurar permisos.
- Mantiene el botón de verificación disponible después de reiniciar el bot.

## Requisitos

Para ejecutar el proyecto, necesitas:

- Python 3.10 o superior.
- Una cuenta de Discord.
- Un servidor de Discord donde puedas administrar roles, canales y permisos.
- Acceso al [Discord Developer Portal](https://discord.com/developers/applications).
- Git, si deseas clonar el repositorio.
- Una conexión a Internet.

## Crear y configurar el bot en Discord

1. Ingresa al [Discord Developer Portal](https://discord.com/developers/applications).
2. Crea una aplicación con el botón **New Application**.
3. Entra a la sección **Bot** y crea el usuario del bot si todavía no existe.
4. Copia el token del bot, lo necesitarás para configurar el archivo `.env`.
5. En la sección **Bot**, activa el intent privilegiado **Server Members Intent**.
6. En **OAuth2 - URL Generator**, selecciona los ámbitos `bot` y `applications.commands`.
7. Selecciona los permisos necesarios para el bot y utiliza la URL generada para invitarlo a tu servidor.

**Permisos necesarios del bot:**

- Manage Roles (Administrar roles).
- Kick Members (Expulsar miembros).
- Manage Channels (Administrar canales).
- View Channels (Ver canales).
- Send Messages (Enviar mensajes).
- Embed Links (Insertar enlaces).
- Read Message History (Leer historial de mensajes).

El bot también necesita que sus permisos efectivos le permitan leer y enviar mensajes en el canal de verificación.

**Importante:** el rol más alto del bot debe estar por encima de los roles `No verificado` y `Verificado` en la jerarquía del servidor.

## Crear un entorno virtual

Desde la terminal, dentro de la carpeta del proyecto, ejecuta:

```bash
python -m venv .venv
```

Activa el entorno virtual en Windows:

**CMD:**

```bat
.venv\Scripts\activate
```

**PowerShell:**

```powershell
.\.venv\Scripts\Activate.ps1
```

Cuando se active correctamente, normalmente verás `(.venv)` al inicio de la línea de la terminal.

## Instalar las dependencias

Con el entorno virtual activado, ejecuta:

```bash
python -m pip install --upgrade pip
python -m pip install discord.py python-dotenv
```

Estas librerías permiten conectarse a Discord y cargar las variables de configuración desde el archivo `.env`.

## Configurar el archivo .env

```env
DISCORD_TOKEN= Coloca aquí el token de tú aplicación de Discord
```

## Configurar el archivo .gitignore

```gitignore
.env
.venv/
__pycache__/
*.py[cod]
verificaciones.db
```

La base de datos `verificaciones.db` se utiliza para conservar los registros de verificación. No la borres mientras el bot esté funcionando si necesitas mantener los plazos registrados.

## Iniciar el bot

Comprueba que estás en la carpeta del proyecto y que el entorno virtual está activado.

Ejecuta:

```bash
python bot.py
```

Si todo está configurado correctamente, en la terminal aparecerá un mensaje indicando que el bot se conectó a Discord y que los comandos slash se sincronizaron.

Para detenerlo, presiona `Ctrl + C` en la terminal.

**Nota:** el bot debe permanecer ejecutándose para gestionar los vencimientos y procesar las verificaciones automáticamente. Si cierras la terminal, el proceso puede detenerse.

## Comandos disponibles

Los comandos slash disponibles en el proyecto son:

| Comando | Función |
|---|---|
| `/diagnostico` | Revisa los roles, permisos, canales y configuración del bot. |
| `/estado` | Consulta los miembros pendientes y los plazos vencidos. |
| `/sincronizar` | Revisa los miembros existentes y sus mensajes de verificación. |
| `/configurar_verificacion` | Aplica o revisa la configuración de permisos de verificación. |

Los comandos administrativos deben ejecutarse con una cuenta que tenga los permisos requeridos por el bot.

## Configuración del servidor

Crea los siguientes roles:

- `No verificado`
- `Verificado`

También debes crear el canal:

- `#canal_verificador`

El nombre del canal y los nombres de los roles deben coincidir con los configurados en el código.

La configuración busca que los miembros pendientes solo puedan acceder al canal de verificación, mientras que los miembros verificados puedan acceder a los canales normales del servidor.

Ten presente que los permisos de otros roles pueden modificar la visibilidad efectiva de los canales. Además, los propietarios y los miembros con el permiso Administrador pueden omitir las restricciones normales de canales de Discord.

## Base de datos

El proyecto utiliza SQLite y guarda los datos en:

```text
verificaciones.db
```

El archivo se crea automáticamente cuando el bot inicializa la base de datos.

Se utiliza para conservar los registros pendientes y la información relacionada con los avisos de verificación. No es necesario instalar un servidor de base de datos adicional.

## Solución de problemas

**El bot no inicia**

- Comprueba que el entorno virtual esté activado.
- Verifica que las dependencias estén instaladas.
- Revisa que el archivo `.env` exista y que el token sea válido.

**El bot se conecta, pero no puede asignar roles**

- Comprueba que tenga el permiso Administrar roles.
- Verifica la jerarquía de roles.
- Confirma que los nombres de los roles coincidan con los configurados en el código.

**No aparecen los comandos slash**

- Comprueba que el bot fue invitado con el ámbito `applications.commands`.
- Espera unos segundos y revisa la terminal.
- Ejecuta nuevamente el bot y comprueba que no aparezcan errores de sincronización.

**La sincronización falla**

- Revisa los errores de la terminal.
- Comprueba que el bot tenga acceso al servidor y al canal de verificación.
- Ejecuta `/diagnostico` para revisar la configuración.

**Aparecen advertencias sobre PyNaCl o davey**

Estas advertencias se relacionan con funciones de voz. No impiden necesariamente que funcionen las funciones de verificación por texto.