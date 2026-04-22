# 🎬 Cartelera València

> Cartelera agregada de Babel, Lys, ABC, Yelmo y Kinépolis — actualización diaria automática.

[![Scrape Showtimes](https://github.com/nilsbeck/cartelera-valencia/actions/workflows/scrape.yml/badge.svg)](https://github.com/nilsbeck/cartelera-valencia/actions/workflows/scrape.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**[→ Ver cartelera](https://nilsbeck.github.io/cartelera-valencia)**

Filtra por idioma (VO / ES / VAL), cine, y día. Las preferencias se guardan en una cookie de 30 días. Tráilers en modal. Sesiones enlazan directamente a la web de compra.

---

## Arquitectura

```
GitHub Actions (cron 09:00 Madrid)
  │
  ├─ scraper/run.py
  │     ├─ cinemas/babel.py
  │     ├─ cinemas/lys.py
  │     ├─ cinemas/abc.py
  │     ├─ cinemas/yelmo.py
  │     └─ cinemas/kinepolis.py
  │
  ├─ scraper/tmdb.py          → poster, rating, duración, géneros, tráiler
  │
  ├─ data/showtimes.json      ← commiteado de vuelta al repo
  ├─ posters/*.jpg            ← descargados de TMDB
  │
  └─ GitHub Pages → index.html (lee data/showtimes.json)
```

El frontend es HTML/CSS/JS puro — sin framework, sin build step. Funciona directamente desde GitHub Pages.

---

## Setup

### 1. Fork / clone

```bash
git clone https://github.com/nilsbeck/cartelera-valencia.git
cd cartelera-valencia
```

### 2. TMDB API Key (gratis)

1. Regístrate en [themoviedb.org](https://www.themoviedb.org/settings/api)
2. Ve a **Settings → API → Create → Developer**
3. Copia la API Key (v3 auth)
4. En GitHub: **Settings → Secrets → Actions → New repository secret**
   - Name: `TMDB_API_KEY`
   - Value: tu clave

### 3. Habilitar GitHub Pages

Settings → Pages → Source: **Deploy from branch** → `main` / `/ (root)`

El sitio quedará en `https://nilsbeck.github.io/cartelera-valencia`

### 4. Ajustar selectores CSS de los scrapers

Cada `scraper/cinemas/*.py` contiene selectores de plantilla que **debes actualizar** inspeccionando el HTML real de cada cine. Abre las DevTools en cada URL y busca los elementos de título, idioma y horarios.

| Cine       | URL cartelera |
|------------|---------------|
| Babel      | https://www.cinebabel.com/cartelera |
| Lys        | https://www.cinelys.es/cartelera |
| ABC        | https://www.cinesabc.com/cartelera |
| Yelmo      | https://www.yelmocinemas.es/peliculas-en-cartelera/valencia |
| Kinépolis  | https://kinepolis.es/cines/kinepolis-valencia |

### 5. Primer run local

```bash
cd scraper
pip install -r requirements.txt
playwright install chromium

TMDB_API_KEY=tu_clave python run.py
# Escribe data/showtimes.json y descarga posters/
```

O dispara manualmente desde GitHub: **Actions → Scrape Showtimes → Run workflow**

---

## Normalización de idiomas

El scraper mapea las etiquetas heterogéneas de cada cine a tres valores canónicos:

| Valores scrapeados | Normalizado |
|--------------------|-------------|
| `VO`, `VOSE`, `V.O.`, `VOS`, `V.O.S.E.`, `Original` | `VO` |
| `Castellano`, `Español`, `ESP`, `Doblada`, `Doblado` | `ES` |
| `Valencià`, `Valenciano`, `VAL`, `En valencià` | `VAL` |

Cualquier valor no reconocido cae en `ES` por defecto.

---

## Estructura de datos

`data/showtimes.json`:

```jsonc
{
  "updated_at": "2026-04-21T07:00:00Z",
  "movies": [
    {
      "id": "tmdb-12345",
      "title": "A Complete Unknown",
      "title_local": "Un Completo Desconocido",
      "poster": "posters/a-complete-unknown.jpg",
      "rating": 7.6,
      "duration": 140,
      "genres": ["Drama", "Biopic"],
      "trailer_youtube_id": "abc123",
      "showtimes": [
        {
          "cinema": "babel",
          "language": "VO",
          "date": "2026-04-21",
          "time": "20:30",
          "url": "https://..."
        }
      ]
    }
  ]
}
```

---

## Tests

```bash
cd scraper
pip install pytest
pytest tests/ -v
```

Los tests cubren normalización de idiomas, slugify, deduplicación de sesiones, y validación del schema JSON. Los scrapers individuales no se testean en CI (requieren red), pero hay fixtures con HTML de ejemplo en `tests/fixtures/`.

---

## Desarrollo

```bash
# Añadir un nuevo cine
cp scraper/cinemas/babel.py scraper/cinemas/nuevo_cine.py
# Edita la URL y los selectores
# Añade la entrada en scraper/run.py
# Añade el toggle en index.html
# Añade tests en tests/test_scrapers.py
```

---

## Licencia

[MIT](LICENSE) — úsalo, fórkalo, mejóralo.
