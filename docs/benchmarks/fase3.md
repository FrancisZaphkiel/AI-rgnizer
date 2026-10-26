# Fase 3 — Evaluación del modelo de destino

Fecha: 2026-10-25 · CachyOS · Python 3.14 · scikit-learn (versión de `uv.lock`).

## Conjunto

Archivos reales del usuario en `~/Documentos`, `~/Música`, `~/Imágenes` y `~/Vídeos`
(nunca versionados). La etiqueta de cada archivo es su carpeta actual truncada a
`max_depth = 2` bajo la raíz.

| | |
|---|---|
| Archivos indexados | ≈ 164 500 |
| Ignorados en proyectos de software | 161 086 (37 proyectos: repos, venvs, RLCraft…) |
| Ejemplos | 3 328 en 33 carpetas (las de < 5 archivos se descartan) |
| Partición | 80 % entrenamiento / 20 % prueba, estratificada |

## Resultados (`airgnizer evaluate`)

| Métrica | Valor | Objetivo |
|---|---|---|
| Cobertura (no se abstiene) | 97,9 % | — |
| Top‑1 sobre no abstenidas | **96,7 %** | ≥ 70 % |
| Top‑3 sobre no abstenidas | **98,7 %** | ≥ 90 % |
| Decididas por regla | 348 (346 correctas) | — |
| ECE (10 tramos) | **0,017** | bajo |

Tiempos: construir el conjunto ≈ 71 s la primera vez (extracción de características; luego
sale de la caché), evaluación ≈ 8 s, `review` (5 modelos) ≈ 2 min 45 s.

## Observaciones

- La precisión es alta en parte porque muchas carpetas son homogéneas (capturas, música,
  wallpapers). En Descargas, con archivos que no se parecen a nada organizado, el modelo se
  abstiene con más frecuencia; es el comportamiento buscado.
- `camerapy/capturas` (salida de un programa) dominaba los `.jpg` con una regla
  `ext:jpg → camerapy/capturas`. Solución: `learning.exclude` en `config.toml`.
- `review` sobre los mismos datos propone 22 de 2 073 archivos revisables, la mitad
  canciones en `Vídeos/musica pendrive` (destino `Música`).

## Reproducir

```fish
uv run airgnizer evaluate ~/Documentos ~/Música ~/Imágenes ~/Vídeos
```
