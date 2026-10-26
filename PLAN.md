# Plan de Proyecto: AI-rgnizer, organizador inteligente de archivos

## 1. Objetivo

Construir una aplicación de escritorio para Linux (probada en **CachyOS**, con KDE/GNOME en Wayland/X11) que:

1. **Aprenda las costumbres del usuario**: nomenclatura de archivos y directorios, estructura de organización y destinos habituales.
2. **Prediga el destino** de archivos que llegan a `~/Descargas` (u otras carpetas vigiladas) y sugiera o ejecute el movimiento.
3. **Detecte archivos duplicados** (idénticos y casi idénticos) y permita enviar los sobrantes a la **papelera** (nunca borrado inmediato; el vaciado queda a decisión del usuario).
4. Ofrezca una **interfaz gráfica** clara para revisar sugerencias, aceptar/rechazar acciones y deshacerlas.

### Principios
- **Local y privado**: todo el aprendizaje y los datos se quedan en el equipo; sin servicios en la nube.
- **Seguro por defecto**: ninguna acción destructiva; todo movimiento es reversible (historial + deshacer); eliminación solo vía papelera (estándar freedesktop.org).
- **El usuario decide**: modo "sugerir" por defecto; modo automático solo opt-in y con umbral de confianza.
- **El modelo solo propone**: el motor de sugerencias nunca toca archivos; toda operación (GUI, CLI o daemon) pasa por la misma capa de acciones seguras.

---

## 2. Tecnologías

| Área | Tecnología | Motivo |
|---|---|---|
| Lenguaje | **Python 3.12+** | Ecosistema ML maduro, disponible nativamente en CachyOS |
| Gestión de entorno | **uv** + `pyproject.toml` | Rápido, reproducible |
| Interfaz gráfica | **PySide6 (Qt 6)** + QML/Widgets | Nativo en Linux, buen soporte Wayland, integra bien con KDE (default de CachyOS) |
| Bandeja / notificaciones | `QSystemTrayIcon`, notificaciones D-Bus (`org.freedesktop.Notifications`) | Avisos cuando llega un archivo nuevo |
| Vigilancia de carpetas | **watchdog** (inotify) | Detecta archivos nuevos en Descargas |
| Base de datos | **SQLite** (modo WAL) + **SQLAlchemy 2** + **Alembic** | Historial, índice de archivos, hashes, feedback; un único proceso escritor (el daemon) |
| Machine Learning | **scikit-learn** (`SGDClassifier(loss="log_loss")` + calibración; kNN/centroides como alternativa para clases nuevas) | Ligero, entrenable en local; LightGBM aplazado (no admite `partial_fit`) hasta demostrar mejora |
| Features de texto | `HashingVectorizer` de n‑gramas de caracteres sobre nombres | Vocabulario abierto, compatible con aprendizaje incremental (sin TF‑IDF, que exige vocabulario fijo) |
| Metadatos | `python-magic` (MIME), `Pillow` + EXIF, `pypdf`, `mutagen` (audio) | Features según tipo de archivo |
| Origen de descarga | atributos extendidos `user.xdg.origin.url` (`os.getxattr`) | URL de origen como feature **opcional** (Chromium lo escribe; verificar Firefox); nunca se asume presente ni fiable |
| Duplicados exactos | **xxhash** (pre-filtro) + **BLAKE3**/SHA‑256 | Comparación rápida y fiable |
| Duplicados similares | **imagehash** (pHash/dHash) para imágenes; `rapidfuzz` para nombres | Fotos redimensionadas, copias "(1)" |
| Papelera | Implementación propia de la spec **freedesktop Trash 1.0** (`actions/trash.py`) | Archivos recuperables desde Dolphin/Nautilus. Se descartó `send2trash`: lee `XDG_DATA_HOME` al importarse, no devuelve la ruta en la papelera (necesaria para deshacer) y copia entre dispositivos. En subvolúmenes Btrfs (`EXDEV`) usa `$topdir/.Trash-$UID` |
| Patrones de nomenclatura | `re`, `dateparser` | Inferir plantillas tipo `AAAA-MM-DD_proyecto_vN` |
| Servicio en segundo plano | Unidad **systemd --user** | Vigilancia persistente sin GUI abierta |
| Pruebas | **pytest**, `pytest-qt`, `pytest-cov`, `hypothesis`, `pyfakefs` | Unitarias, GUI e integración (lo que toca SQLite usa `tmp_path` real: pyfakefs no intercepta el código C de SQLite) |
| Calidad | **ruff**, **mypy**, **pre-commit** | Estilo y tipado |
| Empaquetado | **PKGBUILD** (Arch/CachyOS), opcional AppImage/Flatpak | Instalación nativa |
| CI | GitHub Actions / GitLab CI (Ubuntu + contenedor Arch) | Tests en cada PR |

### Arquitectura (módulos)

```
airgnizer/
├── src/airgnizer/
│   ├── core/          # modelos de dominio, config, rutas XDG
│   ├── scanner/       # indexado del árbol de directorios del usuario
│   ├── watcher/       # vigilancia de Descargas (watchdog)
│   ├── features/      # extracción de características (nombre, MIME, metadatos, origen)
│   ├── learning/      # modelo de destino, patrones de nomenclatura, feedback
│   ├── duplicates/    # hashing, agrupación, similitud
│   ├── actions/       # mover, renombrar, papelera, deshacer (journal)
│   ├── storage/       # SQLite, migraciones
│   ├── daemon/        # servicio systemd + D-Bus
│   └── gui/           # PySide6: ventanas, modelos Qt, bandeja
├── tests/
│   ├── unit/ integration/ gui/ fixtures/
├── packaging/         # PKGBUILD, .desktop, unidad systemd
└── docs/
```

### Flujo de dependencias

```
GUI / CLI ──▶ Servicios de aplicación ──┬──▶ Motor de sugerencias (reglas, modelo, feedback)
                                        └──▶ Motor de archivos (índice, hashes, metadatos)
                         ambos ──▶ Capa de acciones seguras (validación, journal, mover, papelera, recuperación)
                                        ──▶ Sistema de archivos + SQLite
```

Regla: solo `actions/` puede modificar el sistema de archivos; `learning/` devuelve destinos y puntuaciones, nunca ejecuta.

### Requisitos transversales
- **Concurrencia**: bloqueo por ruta en la capa de acciones; dos operaciones no pueden actuar sobre el mismo archivo a la vez.
- **Recuperación tras fallos**: al arrancar se reconcilian las acciones del journal en estado `pending`/`in_progress`.
- **Límites de recursos**: escaneo y hashing con prioridad baja (`os.nice`, `ionice` idle) y tamaño de lote acotado.
- **Metadatos no fiables**: xattrs, EXIF y metadatos PDF/audio son señales opcionales; su ausencia o corrupción nunca provoca error.
- **Observabilidad**: logs estructurados, contadores de errores y modo diagnóstico que anonimiza rutas.

---

## 3. Historias de Usuario

### Épica A — Aprendizaje de costumbres
- **HU-01** Como usuario, quiero que la aplicación **analice mis carpetas existentes** (p. ej. `~/Documentos`, `~/Imágenes`) para que aprenda cómo organizo mis archivos sin que tenga que configurarla manualmente.
  - *Criterios*: el usuario elige qué carpetas indexar y cuáles excluir; el escaneo muestra progreso y puede cancelarse; no modifica ningún archivo.
- **HU-02** Como usuario, quiero que la aplicación **detecte mis patrones de nomenclatura** (fechas, separadores, mayúsculas, prefijos) para que pueda sugerir nombres coherentes.
  - *Criterios*: se muestran los patrones detectados por carpeta con ejemplos; el usuario puede editarlos o descartarlos.
- **HU-03** Como usuario, quiero que el modelo **mejore con mis correcciones** para que sus sugerencias sean cada vez más acertadas.
  - *Criterios*: cada aceptación/rechazo/corrección se registra como feedback y actualiza el modelo de forma incremental.
- **HU-04** Como usuario, quiero **ver por qué** se sugiere un destino (p. ej. "archivos PDF de banco.cl van a Finanzas/Banco") para confiar en la sugerencia.

### Épica B — Predicción de destino de Descargas
- **HU-05** Como usuario, quiero que al **llegar un archivo a Descargas** la aplicación me sugiera a qué carpeta moverlo.
  - *Criterios*: notificación con top‑3 destinos y nivel de confianza; espera a que la descarga termine (ignora `.part`, `.crdownload` y el archivo final de 0 bytes que Firefox crea junto al `.part`); si la confianza es baja, se abstiene y lo deja como "sin clasificar".
- **HU-06** Como usuario, quiero **aceptar, cambiar o ignorar** una sugerencia desde la notificación o la ventana principal.
- **HU-07** Como usuario, quiero un **modo automático opcional** que mueva solo archivos con confianza superior a un umbral configurable.
- **HU-08** Como usuario, quiero **deshacer** cualquier movimiento o renombrado realizado por la aplicación.
- **HU-09** Como usuario, quiero **organizar en lote** los archivos que ya están acumulados en Descargas, revisando una tabla de sugerencias antes de aplicar.
- **HU-10** Como usuario, quiero que se me **sugiera un nombre** acorde a mis convenciones al mover un archivo (opcional).

### Épica C — Duplicados
- **HU-11** Como usuario, quiero **buscar archivos duplicados** en las carpetas que elija para liberar espacio.
  - *Criterios*: detecta duplicados exactos por contenido (no por nombre); muestra grupos, tamaño y espacio recuperable **real** (descontando hardlinks y reflinks de Btrfs, que no liberan espacio).
- **HU-11b** Como usuario, quiero que los **candidatos similares** (pHash, nombre) se muestren separados de los exactos y nunca se envíen a la papelera sin una comparación visual y confirmación explícita.
- **HU-12** Como usuario, quiero detectar **imágenes casi idénticas** (redimensionadas o recomprimidas) con un umbral ajustable.
- **HU-13** Como usuario, quiero que la aplicación **proponga qué copia conservar** (p. ej. la de la ruta más "organizada", la más antigua o de mayor resolución) y poder cambiarlo.
- **HU-14** Como usuario, quiero que los duplicados eliminados **vayan a la papelera** y no se borren de inmediato, para poder recuperarlos.
  - *Criterios*: usa la papelera estándar del sistema (visible en Dolphin/Nautilus); confirmación antes de enviar; nunca se vacía la papelera automáticamente.
- **HU-15** Como usuario, quiero **previsualizar** los archivos de un grupo (miniatura, ruta, fecha, tamaño) antes de decidir.

### Épica D — Interfaz y sistema
- **HU-16** Como usuario, quiero una **interfaz gráfica** con secciones: Panel, Descargas pendientes, Duplicados, Patrones aprendidos, Historial, Ajustes.
- **HU-17** Como usuario, quiero un **icono en la bandeja** que indique archivos pendientes y permita abrir la app.
- **HU-18** Como usuario, quiero que la vigilancia **arranque con mi sesión** (servicio systemd de usuario), configurable.
- **HU-19** Como usuario, quiero que la interfaz respete el **tema claro/oscuro** del sistema y funcione en Wayland.
- **HU-20** Como usuario, quiero **exportar/borrar** los datos aprendidos para controlar mi privacidad.

---

## 4. Estrategia de Ramas (Git Flow)

Ramas permanentes:
- `main` — versiones estables etiquetadas (`v0.1.0`, `v0.2.0`, …).
- `develop` — integración continua de features.

Ramas de soporte (se crean desde `develop`, se fusionan a `develop` vía PR con tests en verde):

| Fase | Rama | Contenido |
|---|---|---|
| 0 | `feature/setup-proyecto` | Estructura, `pyproject.toml`, uv, ruff, mypy, pre-commit, CI |
| 1 | `feature/storage-db` | Modelos SQLAlchemy, migraciones Alembic |
| 1 | `feature/scanner` | Indexado de directorios y metadatos |
| 1 | `feature/acciones-seguras` | Mover/renombrar con journal, deshacer, papelera |
| 2 | `feature/duplicados-exactos` | Hashing por etapas y agrupación |
| 2 | `feature/duplicados-similares` | pHash de imágenes, similitud de nombres |
| 3 | `feature/extraccion-features` | Features de nombre, MIME, metadatos, origen |
| 3 | `feature/modelo-destino` | Clasificador incremental y evaluación |
| 3 | `feature/patrones-nomenclatura` | Inferencia de plantillas de nombres |
| 4 | `feature/watcher-descargas` | Vigilancia inotify y cola de sugerencias |
| 4 | `feature/daemon-systemd` | Servicio de usuario y D-Bus |
| 5 | `feature/gui-base` | Ventana principal, navegación, bandeja |
| 5 | `feature/gui-descargas` | Vista de sugerencias y feedback |
| 5 | `feature/gui-duplicados` | Vista de grupos, previsualización, papelera |
| 5 | `feature/gui-historial-ajustes` | Historial, deshacer, ajustes, privacidad |
| 6 | `feature/empaquetado` | PKGBUILD, `.desktop`, unidad systemd |

Otras ramas:
- `release/x.y.0` — al cerrar cada hito: estabilización, versión y changelog; se fusiona a `main` y `develop` y se etiqueta.
- `hotfix/<descripcion>` — desde `main` para errores críticos; se fusiona a `main` y `develop`.

Convenciones: commits en estilo *Conventional Commits* (`feat:`, `fix:`, `test:`, `docs:`); PR obligatoria con CI en verde y cobertura ≥ 80 % en módulos de núcleo.

Hitos de release:
- `v0.1.0` — Fases 0–2 (núcleo + duplicados, CLI).
- `v0.2.0` — Fases 3–4 (aprendizaje + vigilancia).
- `v0.3.0` — Fase 5 (GUI completa).
- `v1.0.0` — Fase 6 (empaquetado y estabilización).

---

## 5. Fases de Trabajo

Cada fase termina con una **batería de pruebas obligatoria**; la fase no se cierra (ni se fusiona su release) si alguna falla.

### Fase 0 — Preparación del proyecto
**Tareas**
- Inicializar estructura `src/`, `pyproject.toml` con uv, dependencias base.
- Configurar ruff, mypy, pre-commit, pytest y CI.
- Configuración con rutas XDG (`~/.config/airgnizer`, `~/.local/share/airgnizer`).
- Documentar en `README.md` cómo instalar dependencias en CachyOS (`pacman -S python pyside6 ...` o vía uv).

**Pruebas de cierre**
- [ ] `uv run pytest` ejecuta un test de humo correctamente.
- [ ] `ruff check` y `mypy` sin errores.
- [ ] CI corre y pasa en un PR de prueba.
- [ ] Test: la configuración crea rutas XDG correctas en un `HOME` temporal.

### Fase 1 — Núcleo: almacenamiento, escaneo y acciones seguras (HU-01, HU-08, HU-14)
**Tareas**
- Esquema SQLite (WAL): `files`, `directories`, `actions_journal`, `feedback`, `patterns`, `settings`, `scan_errors`.
- Scanner recursivo con exclusiones (ocultos, `node_modules`, `.git`, montajes externos opcionales), incremental por `mtime`+`ctime`+tamaño+inode; registra errores de lectura y permisos; trata symlinks y montajes de forma explícita.
- Reconciliación periódica (re-escaneo) además de los eventos; ante duda, invalidar hashes.
- Capa de acciones: `move`, `rename`, `trash` con **journal como máquina de estados**:
  - estados `pending → in_progress → done → reverted` (y `failed`), con id de operación, rutas absolutas, tamaño, mtime, inode y hash del archivo;
  - el registro se escribe (y hace commit) **antes** de tocar el archivo;
  - recuperación al arrancar: las operaciones `pending`/`in_progress` se completan o revierten según el estado real del disco.
- `undo` seguro: verifica que el archivo sigue donde se espera y no ha cambiado (tamaño/mtime/hash); nunca sobrescribe el origen si ya existe algo ahí (propone nombre alternativo).
- Movimiento entre sistemas de archivos (incluido entre subvolúmenes Btrfs): no se asume atómico; la función auditada `fsops.copy_verify_unlink` copia a un temporal, hace `fsync`, compara hash, publica con `renameat2(RENAME_NOREPLACE)` y solo entonces elimina el origen. Todos los borrados viven en `actions/fsops.py` (ruff `TID251` los prohíbe en el resto).
- Bloqueo por ruta para impedir operaciones concurrentes sobre el mismo archivo.
- Manejo de colisiones de nombre y permisos.

**Pruebas de cierre**
- [ ] Pruebas del scanner (exclusiones, symlinks, ciclos, montajes, archivos sin permiso).
- [ ] Escaneo incremental: solo reprocesa archivos modificados.
- [ ] Mover + deshacer restaura ruta y nombre exactos (propiedad verificada con `hypothesis`).
- [ ] `trash` deja el archivo en la papelera freedesktop correspondiente (`~/.local/share/Trash` o `$topdir/.Trash-$UID` en otro subvolumen/volumen) con su `.trashinfo`; caso de subvolumen Btrfs cubierto.
- [ ] `os.remove`/`unlink` (incluido `Path.unlink`) solo se invocan desde `airgnizer.actions.fsops` (test que audita cada llamada); ningún archivo del usuario se borra: solo origenes ya copiados y verificados, temporales propios y `.trashinfo` tras restaurar.
- [ ] Movimiento entre sistemas de archivos con fallo inyectado tras la copia no pierde el origen.
- [ ] Inyección de fallos (corte en cada estado del journal) + reinicio: la recuperación deja cada archivo en el origen o en el destino, nunca perdido ni duplicado.
- [ ] `undo` se niega (sin daño) si el archivo fue modificado o si el origen está ocupado.
- [ ] Dos operaciones concurrentes sobre el mismo archivo: una se rechaza.
- [ ] Colisión de nombres no sobrescribe archivos existentes.
- [ ] Migraciones Alembic suben y bajan sin pérdida.

### Fase 2 — Detección de duplicados (HU-11, HU-12, HU-13, HU-15)
**Tareas**
- Pipeline por etapas: agrupar por tamaño → hash parcial (primeros/últimos 64 KiB con xxhash, **solo como filtro**) → hash completo BLAKE3 (única confirmación válida).
- Caché de hashes en BD invalidada por `mtime`/tamaño/inode.
- Similitud de imágenes con pHash y umbral de distancia de Hamming; resultados como **candidatos**, en una categoría separada de los exactos; la similitud de nombres nunca es prueba de duplicación.
- Heurística de "copia a conservar" (siempre recomendación editable): ruta dentro de carpeta organizada > fuera de Descargas > nombre sin sufijo `(1)`/`copia` > más antigua; para imágenes, resolución, formato y calidad.
- Hardlinks (mismo inode) y reflinks/extents compartidos de Btrfs (vía `FIEMAP`) se marcan aparte y no cuentan como espacio recuperable.
- CLI provisional: `airgnizer dupes <ruta>`, `airgnizer similar <ruta>` (imágenes) y `airgnizer names <ruta>` (nombres parecidos: solo indicios, sin papelera).

**Pruebas de cierre**
- [x] Fixtures con duplicados exactos, mismo tamaño distinto contenido, archivos vacíos, archivos grandes (>1 GiB generado sparse).
- [x] Cero falsos positivos en duplicados exactos.
- [x] Imágenes redimensionadas/recomprimidas se agrupan; imágenes distintas no (umbral validado).
- [x] Hardlinks no se proponen para eliminar; reflinks no suman espacio recuperable.
- [x] Ningún candidato similar se envía a la papelera sin confirmación explícita por grupo.
- [x] Heurística de conservación elige la copia esperada en casos de prueba.
- [x] Enviar sobrantes a papelera y restaurarlos funciona de extremo a extremo.
- [x] Benchmark (condiciones documentadas: equipo, SSD, distribución de tamaños), medido por separado:
  - escaneo inicial de metadatos de 50 000 archivos: objetivo < 2 min;
  - escaneo incremental sin cambios: objetivo < 15 s;
  - hashing: reportado en MB/s (no comparable con el escaneo).
  - Resultados: [`docs/benchmarks/fase2.md`](docs/benchmarks/fase2.md) (2,3 s y 1,0 s en el árbol sintético; 12,5 s de incremental con 162 637 archivos reales).

**Release `v0.1.0`**

### Fase 3 — Aprendizaje de costumbres (HU-02, HU-03, HU-04, HU-10)
**Tareas**
- Extracción de features por archivo: tokens del nombre, n‑gramas de caracteres, extensión, MIME, tamaño, fechas, EXIF, metadatos PDF/audio, dominio de origen (`user.xdg.origin.url`), palabras clave del contenido (opcional, PDFs de texto).
- Dataset de entrenamiento inicial: archivos ya organizados → etiqueta = directorio destino (relativo, **con profundidad limitada** o predicción jerárquica para evitar cientos de clases con pocos ejemplos). Se asume ruido: se descartan duplicados y carpetas con muy pocos ejemplos.
- Modelo: primero reglas explícitas (extensión/MIME, dominio de origen, palabras clave) con prioridad cuando su soporte es alto; después `SGDClassifier(loss="log_loss")`.
- **Clases nuevas**: `partial_fit` exige declarar todas las clases en la primera llamada; cuando aparece una carpeta nueva se reentrena el modelo completo en segundo plano (barato a esta escala). Evaluar kNN/centroides sobre vectores hasheados como alternativa.
- **Calibración** de probabilidades (`CalibratedClassifierCV` o isotónica sobre datos separados) y **abstención**: por debajo de un umbral, no se sugiere destino.
- CLI `airgnizer suggest <ruta>` para validar el modelo antes de que exista la GUI.
- Explicabilidad: top features / regla que motivó la predicción.
- Inferencia de patrones de nombres por carpeta (plantillas con tokens `{fecha}`, `{texto}`, `{n}`, separadores, *case*).
- Ciclo de feedback: aceptar/corregir actualiza modelo y reglas; versionado del modelo para revertir.

**Pruebas de cierre**
- [x] Unitarias de cada extractor de features (incluyendo archivos corruptos o sin metadatos).
- [x] Árbol sintético de "usuario" (fixtures): verifica comportamientos conocidos (reglas, pipeline), no precisión.
- [x] Evaluación offline sobre un **conjunto real y privado** (archivos propios, etiquetas verificadas, separado del entrenamiento, nunca versionado): **top‑1 ≥ 70 %, top‑3 ≥ 90 %** sobre las predicciones no abstenidas, reportando también la tasa de abstención.
- [x] Calibración medida (curva de fiabilidad / ECE) antes de habilitar el modo automático.
- [x] Añadir una carpeta destino nueva y corregir hacia ella hace que el modelo la prediga (reentrenamiento).
- [x] Tras N correcciones del mismo tipo, el modelo cambia su predicción (test de aprendizaje incremental).
- [x] El inferidor de patrones reconoce `2024-05-10_informe_v2.pdf` → `{fecha:AAAA-MM-DD}_{texto}_v{n}`.
- [x] La explicación generada referencia features reales del archivo.
- [x] El modelo persiste y se recarga dando las mismas predicciones.
- [x] Ningún dato sale del equipo: test que bloquea sockets `AF_INET`/`AF_INET6` (los sockets Unix de D-Bus siguen permitidos).

**Resultado (2026-10-25)**: evaluación real en [`docs/benchmarks/fase3.md`](docs/benchmarks/fase3.md)
— top‑1 96,7 %, top‑3 98,7 %, abstención 2,1 %, ECE 0,017. Las etiquetas son la carpeta en la
que ya está cada archivo (no verificadas una a una); `airgnizer review` ayuda a detectar
las que están mal y conviene repetir la evaluación tras limpiarlas.

### Fase 4 — Vigilancia de Descargas y servicio (HU-05, HU-07, HU-09, HU-18)
**Tareas**
- Watcher con watchdog sobre `~/Descargas` (ruta detectada vía `xdg-user-dir DOWNLOAD`).
- Detección de descarga completa: ignorar temporales (`.part`, `.crdownload`) y archivos de 0 bytes con un `.part` hermano; esperar estabilidad de tamaño.
- Los eventos de inotify actualizan el índice, pero no son la única fuente de verdad: se mantiene la reconciliación periódica.
- Cola de sugerencias en BD; modo sugerir / automático con umbral sobre probabilidad **calibrada**.
- Servicio `systemd --user` como **único escritor** de la BD; API D-Bus para que la GUI consulte y envíe acciones (protocolo versionado y documentado).
- Notificaciones de escritorio con acciones ("Mover a X", "Elegir…", "Ignorar").

**Pruebas de cierre**
- [x] Integración: crear un archivo en un directorio temporal vigilado genera una sugerencia.
- [x] Archivos `.part` / `.crdownload` y el placeholder de 0 bytes de Firefox no generan sugerencia hasta completarse.
- [x] GUI y daemon abiertos a la vez no producen `database is locked` ni escrituras perdidas.
- [x] Modo automático solo mueve con confianza ≥ umbral; el resto queda pendiente.
- [x] El servicio reinicia tras fallo (`Restart=on-failure`) y no duplica sugerencias.
- [x] Llamadas D-Bus de prueba (listar, aceptar, deshacer) responden correctamente.
- [x] Prueba manual en CachyOS: notificación visible en KDE Plasma (Wayland).

**Release `v0.2.0`**

### Fase 5 — Interfaz gráfica (HU-06, HU-15, HU-16, HU-17, HU-19, HU-20)
**Tareas**
- Ventana principal PySide6 con navegación lateral:
  - **Panel**: resumen (pendientes, espacio recuperable, precisión del modelo).
  - **Descargas**: tabla de archivos con destino sugerido, confianza, explicación; aceptar/cambiar/ignorar individual o en lote.
  - **Duplicados**: grupos con miniaturas, copia a conservar marcada, botón "Enviar a papelera" con confirmación.
  - **Patrones**: carpetas y plantillas aprendidas, editables.
  - **Historial**: acciones con botón "Deshacer".
  - **Ajustes**: carpetas vigiladas/indexadas/excluidas, umbral, modo automático, arranque con sesión, exportar/borrar datos.
- Icono de bandeja con contador.
- Operaciones largas en hilos (`QThreadPool`) para no bloquear la UI.
- Tema del sistema, escalado HiDPI, Wayland.

**Pruebas de cierre**
- [x] `pytest-qt`: cada vista carga con datos simulados.
- [x] Flujo GUI: aceptar sugerencia → archivo movido → aparece en historial → deshacer lo restaura.
- [x] Flujo GUI: grupo de duplicados → enviar a papelera → archivos en papelera, no borrados.
- [x] El diálogo de confirmación aparece siempre antes de enviar a papelera.
- [x] La UI no se bloquea durante un escaneo largo (test con temporizador).
- [x] "Borrar datos aprendidos" elimina BD y modelo.
- [x] Prueba manual en CachyOS (KDE Wayland): tema oscuro, bandeja, HiDPI.

**Release `v0.3.0`**

### Fase 6 — Empaquetado y estabilización
**Tareas**
- ~~`PKGBUILD` para instalar con `makepkg -si`~~ → **AppImage** autocontenida (decisión del
  usuario: un solo archivo descargable), con `.desktop`, icono y unidad systemd que apunta a
  la propia AppImage. Renombrado del proyecto a **AI-rgnizer** con migración de datos.
- Logs rotativos en `~/.local/state/airgnizer`.
- Documentación de usuario y de desarrollo.
- Corrección de errores reportados en uso real durante 1–2 semanas.

**Pruebas de cierre**
- [x] La AppImage se construye (`packaging/appimage/build.sh`) y su contenido funciona
  aislado del repositorio (CLI, escaneo, duplicados, GUI).
- [ ] La app aparece en el menú de aplicaciones y arranca (prueba manual en KDE).
- [x] Desinstalar no deja el servicio activo (`service uninstall`, `desktop uninstall`).
- [x] Suite completa de tests en verde; cobertura global ≥ 80 % (86 %, 356 tests).
- [ ] Prueba de aceptación: uso diario en el equipo CachyOS sin pérdida de archivos.

**Release `v1.0.0`**

---

## 6. Riesgos y Mitigaciones

| Riesgo | Mitigación |
|---|---|
| Mover archivos por error | Modo sugerir por defecto, journal con estados + deshacer, abstención, umbral alto sobre probabilidad calibrada |
| Interrupción a mitad de operación | Journal escrito antes de actuar, recuperación al arrancar, copia-verificación-borrado entre sistemas de archivos |
| El modelo aprende de carpetas desordenadas | Reglas primero, filtrado de ruido, evaluación con datos reales separados |
| Índice desactualizado | Eventos + reconciliación periódica, invalidación conservadora de hashes |
| Espacio recuperable sobrestimado (Btrfs) | Detección de hardlinks y reflinks |
| Calendario | Estimación orientativa: 2–4 meses a tiempo completo para una beta; aproximadamente el doble a tiempo parcial |
| Pérdida de datos al eliminar duplicados | Solo papelera, confirmación, nunca vaciado automático, test que prohíbe `unlink` |
| Pocos datos para entrenar (arranque en frío) | Reglas por extensión/MIME como respaldo; aprender rápido con feedback |
| Rendimiento en árboles grandes | Escaneo incremental, caché de hashes, hilos de fondo |
| Diferencias entre entornos (KDE/GNOME, Wayland/X11) | Usar estándares freedesktop (notificaciones, papelera, XDG); pruebas manuales en ambos |
| Privacidad | Todo local, opción de exportar/borrar datos |

---

## 7. Próximos pasos inmediatos

1. Desde `develop`: `git flow feature start setup-proyecto`.
2. Completar la Fase 0 y abrir el primer PR hacia `develop`.
3. Preparar fixtures de un "árbol de usuario" sintético, que se reutilizarán en las fases 1–3.
