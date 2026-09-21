# La lista — backend + panel admin

App de pilotos y campeonatos para el fin de semana de carreras en Balcarce.
Este repo es el **lado servidor**: la API que consume la PWA, el panel de
administración y el cobro por Mercado Pago.

```
la-lista/
├── README.md
├── scraper.py            # importador de arranque (una sola vez)
├── netlify.toml          # deploy de la PWA
├── .gitignore
├── web/
│   └── index.html        # la PWA (hoy con datos de ejemplo embebidos)
└── backend/
    ├── main.py           # API FastAPI + Mercado Pago
    ├── db.py             # Mongo (motor) con fallback en memoria
    ├── admin.html        # panel /admin (clave + ABM pilotos + avisos + cronograma)
    ├── seed.py           # carga inicial desde el scraper
    ├── requirements.txt
    ├── .env.example
    └── render.yaml
```

> **Estado actual:** el backend + panel están completos y probados. La PWA de
> `web/` todavía tiene los datos de ejemplo embebidos; el paso que falta es
> cablearla a `PUBLIC_API_URL/api/data` (pilotos, cronograma, avisos) y sumarle
> la pantalla de pago + service worker. Para eso hace falta la URL del backend
> ya deployado en Render.

## Probar local (sin configurar nada)

```bash
cd backend
pip install -r requirements.txt
ADMIN_TOKEN=probando uvicorn main:app --reload
```

Corre en memoria. Panel en http://127.0.0.1:8000/admin (clave: `probando`).

## Deploy real

**1. Mongo Atlas** — creá un cluster gratis y copiá la connection string
(`mongodb+srv://...`). Es la misma cuenta/estilo que ya usás en tus otros
proyectos.

**2. Backend en Render** — "New → Blueprint", apuntá a este repo (usa
`render.yaml`). Cargá las variables de entorno (ver `.env.example`):
- `ADMIN_TOKEN` — la clave del panel. Poné algo largo.
- `MONGODB_URI` / `MONGODB_DB`
- `PUBLIC_API_URL` — la URL que te da Render (ej. `https://la-lista-api.onrender.com`)
- `PUBLIC_WEB_URL` / `CORS_ORIGINS` — la URL de la PWA en Netlify (paso 5)

**3. Mercado Pago** — en tu panel de developer creá una aplicación y copiá el
**Access Token de producción** (`APP_USR-...`) a `MP_ACCESS_TOKEN`. Poné el
monto en `PRICE_ARS`. El dinero cae a tu cuenta de Mercado Pago; la facturación
(Monotributo) la seguís manejando aparte.

**4. Sembrar la lista (una vez)**
```bash
cd .. && python scraper.py          # genera data/index.json
cd backend && MONGODB_URI="..." python seed.py
```
De acá en más, la lista se edita desde `/admin`. El scraper no se usa más.

**5. PWA en Netlify** — es el último paso y lo cableamos aparte: la app
(`index.html`) deja de tener los datos embebidos y pasa a leer
`PUBLIC_API_URL/api/data`, con la pantalla de pago de Mercado Pago y el
service worker para que instale y ande offline en el box.

## Endpoints

| Método | Ruta | Qué hace |
|---|---|---|
| GET | `/api/data` | categorías + pilotos + campeonato |
| GET | `/api/avisos?cat=` | avisos vigentes |
| POST | `/api/pay` | crea preferencia MP → `init_point` |
| GET | `/api/access?device=` | ¿pagó ese dispositivo? |
| POST | `/api/webhook` | notificaciones de Mercado Pago |
| POST | `/api/admin/login` | valida la clave |
| GET/POST | `/api/admin/pilotos` | listar / crear-editar piloto |
| DELETE | `/api/admin/pilotos/{cat}/{n}` | borrar piloto |
| PUT | `/api/admin/campeonato/{cat}` | actualizar tabla |
| POST/DELETE | `/api/admin/avisos` | publicar / borrar aviso |

Todo lo de `/api/admin/*` pide el header `Authorization: Bearer <ADMIN_TOKEN>`.

## Cómo funciona el cobro

1. La PWA genera un `device` random (queda en el teléfono).
2. Toca "Desbloquear" → `POST /api/pay` → abre el `init_point` de Mercado Pago.
3. El usuario paga en el checkout de MP.
4. MP pega a `/api/webhook`; el backend **le pregunta a MP** por el pago y, solo
   si está `approved`, marca ese `device` como pago. Nunca se confía en el cliente.
5. La PWA consulta `GET /api/access?device=...` y se desbloquea.
