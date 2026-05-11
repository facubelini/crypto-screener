# Deploy en Render.com — Paso a paso

## Por qué Render y no Vercel
Vercel tiene un timeout de 10 segundos en funciones serverless (plan gratis).
El screener necesita ~2-3 minutos para analizar todos los futuros.
Render.com corre el proceso como un servidor real, sin ese límite.

---

## PASO 1 — Subir el código a GitHub

### 1.1 Crear repositorio en GitHub
1. Ir a https://github.com/new
2. Nombre: `crypto-screener` (o el que quieras)
3. Dejarlo **privado** si querés que nadie más lo vea
4. Click en **Create repository**

### 1.2 Inicializar git y subir el código (PowerShell)
Abrir PowerShell en la carpeta `crypto_screener`:

```powershell
cd C:\Users\facundoBelini\Desktop\Claude\crypto_screener

git init
git add .
git commit -m "Initial commit - crypto screener"
git branch -M main
git remote add origin https://github.com/TU_USUARIO/crypto-screener.git
git push -u origin main
```
> Reemplazá `TU_USUARIO` con tu usuario de GitHub.

---

## PASO 2 — Crear cuenta en Render.com

1. Ir a https://render.com
2. Click en **Get Started for Free**
3. Registrarse con GitHub (recomendado, facilita la conexión)

---

## PASO 3 — Crear el servicio web

1. En el dashboard de Render, click en **New +** → **Web Service**
2. Click en **Connect a repository**
3. Seleccionar el repo `crypto-screener` que creaste
4. Render va a detectar automáticamente el `render.yaml`

Si no detecta el render.yaml, configurar manualmente:
- **Name:** crypto-screener
- **Runtime:** Python 3
- **Build Command:** `pip install -r requirements.txt`
- **Start Command:** `gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 300`
- **Plan:** Free

5. Click en **Create Web Service**

---

## PASO 4 — Esperar el deploy (~3-5 minutos)

Render va a:
1. Clonar tu repo
2. Instalar dependencias (`pip install -r requirements.txt`)
3. Arrancar gunicorn

Cuando diga **"Live"** en verde, tu screener está online.

---

## PASO 5 — Acceder a tu screener

La URL va a ser algo como:
```
https://crypto-screener.onrender.com
```
(o el nombre que hayas puesto)

---

## Notas importantes

### Plan gratuito de Render
- El servicio **se apaga** si no recibe tráfico por **15 minutos**
- La primera visita después de estar apagado tarda ~30-60 segundos en arrancar
- Para que no se apague: podés usar https://uptimerobot.com (gratis) para hacer
  un ping cada 5 minutos y mantenerlo activo

### Para actualizar el screener
Cada vez que hagas cambios locales, solo pusheá a GitHub:
```powershell
git add .
git commit -m "descripción del cambio"
git push
```
Render redesplegará automáticamente.
