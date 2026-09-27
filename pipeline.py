#!/usr/bin/env python3
"""
Motor del pipeline: guion -> compases -> imagenes -> voz -> video.

Linea de comandos:
  python pipeline.py nuevo guion.txt      (crea un proyecto nuevo y aislado)
  python pipeline.py escenas guion.txt    (reescribe el proyecto activo)
  python pipeline.py imagenes [--solo 7]
  python pipeline.py voz [--motor edge]
  python pipeline.py montar
  python pipeline.py seo
  python pipeline.py subtitulos

La interfaz web (app.py) usa estas mismas funciones.

IDEA CLAVE: la unidad de AUDIO es la frase, la unidad de IMAGEN es el compas.
Una frase larga se reparte en dos o tres imagenes, pero se locuta de una sola
vez, asi que la narracion suena continua y los dibujos cambian cada 3-6 s.

PROYECTOS: cada video vive en su propia carpeta bajo proyectos/<slug>/, con su
guion, storyboard, imagenes, audio y video. Asi un video nuevo nunca hereda ni
mezcla archivos de uno anterior (misma numeracion de planos, distinto proyecto
= distintas carpetas). `proyectos/activo.txt` guarda cual esta abierto ahora.

Requisitos:
  pip install google-genai pillow requests edge-tts fastapi uvicorn
  GEMINI_API_KEY, ELEVENLABS_API_KEY, ELEVENLABS_VOICE_ID
  ffmpeg en el PATH
"""
import argparse, json, os, pathlib, re, shutil, subprocess, time, unicodedata

REFS = pathlib.Path("referencias")     # estilo, se comparte entre todos los proyectos
EXT_REF = ("*.png", "*.PNG", "*.jpg", "*.JPG", "*.jpeg", "*.JPEG", "*.webp", "*.WEBP")

PROYECTOS = pathlib.Path("proyectos")
ACTIVO_TXT = PROYECTOS / "activo.txt"

# Override local del Voice ID de ElevenLabs. Vive fuera del entorno del SO
# porque un "setx"/variable de sistema no la ven los procesos que este
# programa ya tenia abiertos, solo los que se abran despues de reiniciar
# sesion. Si este fichero existe, manda sobre ELEVENLABS_VOICE_ID.
VOZ_TXT = pathlib.Path("voz.txt")


def voz_configurada():
    return VOZ_TXT.read_text(encoding="utf-8").strip() if VOZ_TXT.exists() else ""

# Rutas del proyecto activo. Se rellenan al importar el modulo (ver _iniciar,
# al final del fichero) y cada vez que se llama a activar_proyecto().
OUT = IMGS = AUDIO = CLIPS = ESCENAS = VIDEO = GUION = SEO = SUBTITULOS = GASTO = None
REF_PROYECTO = TITULO = None


def _rutas(base):
    global OUT, IMGS, AUDIO, CLIPS, ESCENAS, VIDEO, GUION, SEO, SUBTITULOS, GASTO, REF_PROYECTO, TITULO
    OUT = base
    IMGS = OUT / "img"
    AUDIO = OUT / "audio"
    CLIPS = OUT / "clips"
    ESCENAS = OUT / "escenas.json"
    VIDEO = OUT / "video.mp4"
    GUION = OUT / "guion.txt"
    SEO = OUT / "seo.json"
    SUBTITULOS = OUT / "subtitulos.srt"
    GASTO = OUT / "gasto.json"
    REF_PROYECTO = OUT / "referencias"  # diseños propios de este video (animales, objetos)
    TITULO = OUT / "titulo.txt"  # nombre bonito que TU le pones al proyecto, distinto del slug de carpeta


def cargar_titulo():
    """El nombre que el usuario le puso al proyecto (si lo puso); si no,
    cae al nombre de carpeta legible, para que nunca se vea vacio."""
    if TITULO and TITULO.exists():
        t = TITULO.read_text(encoding="utf-8").strip()
        if t:
            return t
    return (OUT.name.replace("-", " ").capitalize()) if OUT else ""


def guardar_titulo(t):
    OUT.mkdir(parents=True, exist_ok=True)
    TITULO.write_text((t or "").strip(), encoding="utf-8")


def slug(texto, palabras=7):
    """Nombre de carpeta legible y unico a partir de las primeras palabras del guion."""
    t = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode()
    t = re.sub(r"[^a-zA-Z0-9\s]", "", t).strip().lower()
    t = "-".join(t.split()[:palabras])
    return t or "video"


def listar_proyectos():
    """Todos los proyectos guardados, el mas reciente primero."""
    if not PROYECTOS.is_dir():
        return []
    out = []
    for p in PROYECTOS.iterdir():
        if not p.is_dir():
            continue
        esc = p / "escenas.json"
        n = len(json.loads(esc.read_text(encoding="utf-8"))) if esc.exists() else 0
        tit = p / "titulo.txt"
        titulo = (tit.read_text(encoding="utf-8").strip() if tit.exists() else "") \
                 or p.name.replace("-", " ").capitalize()
        out.append({
            "nombre": p.name,
            "titulo": titulo,
            "planos": n,
            "video": (p / "video.mp4").exists(),
            "activo": p.name == proyecto_activo(),
            "modificado": p.stat().st_mtime,
        })
    return sorted(out, key=lambda x: x["modificado"], reverse=True)


def proyecto_activo():
    if ACTIVO_TXT.exists():
        n = ACTIVO_TXT.read_text(encoding="utf-8").strip()
        if (PROYECTOS / n).is_dir():
            return n
    return None


def activar_proyecto(nombre):
    """Cambia el proyecto activo: todas las rutas (IMGS, AUDIO, ESCENAS...) pasan
    a apuntar a su carpeta. No toca ni borra nada del proyecto anterior."""
    PROYECTOS.mkdir(exist_ok=True)
    base = PROYECTOS / nombre
    base.mkdir(exist_ok=True)
    _rutas(base)
    ACTIVO_TXT.write_text(nombre, encoding="utf-8")
    return base


def crear_proyecto(texto_guion):
    """Proyecto nuevo y aislado: nunca comparte carpeta ni numeracion con otro,
    asi que jamas hereda imagenes o audio de un video distinto."""
    base_nombre = slug(texto_guion)
    nombre, i = base_nombre, 2
    while (PROYECTOS / nombre).is_dir():
        nombre = f"{base_nombre}-{i}"
        i += 1
    activar_proyecto(nombre)
    GUION.write_text(texto_guion, encoding="utf-8")
    return nombre


def versionar(log=print):
    """Copia con fecha del guion + storyboard actuales, antes de sobrescribirlos.
    No hace nada si el proyecto todavia esta vacio."""
    if not (GUION.exists() or ESCENAS.exists()):
        return None
    sello = time.strftime("%Y%m%d-%H%M%S")
    dest = OUT / "versiones" / sello
    dest.mkdir(parents=True, exist_ok=True)
    for nombre in ("guion.txt", "escenas.json", "biblia.json"):
        origen = OUT / nombre
        if origen.exists():
            shutil.copy2(origen, dest / nombre)
    log(f"copia de seguridad del storyboard anterior en {dest}")
    return dest


# --------------------------------------------------------------------- ideas
IDEAS = PROYECTOS / "ideas.json"
ESTADOS_IDEA = ["idea", "guion listo", "en produccion", "publicado"]


def listar_ideas():
    """Banco de ideas de vídeo, independiente del proyecto activo."""
    if not IDEAS.exists():
        return []
    return json.loads(IDEAS.read_text(encoding="utf-8"))


def _guardar_ideas(ideas):
    PROYECTOS.mkdir(exist_ok=True)
    IDEAS.write_text(json.dumps(ideas, ensure_ascii=False, indent=2), encoding="utf-8")


def anadir_idea(titulo, resumen="", nicho="", notas="", estado="idea", estructura=None):
    ideas = listar_ideas()
    nuevo_id = max((i["id"] for i in ideas), default=0) + 1
    idea = {"id": nuevo_id, "titulo": titulo, "resumen": resumen, "nicho": nicho,
            "notas": notas, "estado": estado if estado in ESTADOS_IDEA else "idea",
            "creado": time.strftime("%Y-%m-%d")}
    if estructura:
        idea["estructura"] = estructura  # objetivo de duracion/ritmo, si viene de una referencia de formato
    ideas.append(idea)
    _guardar_ideas(ideas)
    return idea


def actualizar_idea(idea_id, cambios):
    ideas = listar_ideas()
    for i in ideas:
        if i["id"] == idea_id:
            for k in ("titulo", "resumen", "nicho", "notas", "estado"):
                if k in cambios:
                    i[k] = cambios[k]
            _guardar_ideas(ideas)
            return i
    return None


def borrar_idea(idea_id):
    ideas = [i for i in listar_ideas() if i["id"] != idea_id]
    _guardar_ideas(ideas)


# ---------------------------------------------------- referencia de formato
def analizar_video_referencia(url, transcripcion="", log=print):
    """Analiza un video de YouTube como REFERENCIA DE FORMATO, nunca de
    contenido: saca titulo/canal (datos publicos via oEmbed), y si le pasas
    tu mismo la transcripcion (la copias desde YouTube: tres puntos bajo el
    video -> "Mostrar transcripcion"), analiza su ESTRUCTURA -- cuantos
    bloques tiene, el ritmo, la duracion aproximada. La transcripcion NUNCA
    se guarda ni se repite en el resultado: se usa una sola vez, en memoria,
    solo para sacar estos numeros de formato."""
    import requests
    meta = {}
    try:
        r = requests.get("https://www.youtube.com/oembed",
                          params={"url": url, "format": "json"}, timeout=15)
        r.raise_for_status()
        d = r.json()
        meta = {"titulo": d.get("title", ""), "canal": d.get("author_name", "")}
        log(f"Video de referencia: \"{meta['titulo']}\" ({meta['canal']})")
    except Exception as ex:
        log(f"AVISO: no se pudo leer el video (¿la URL es correcta?): {ex}")

    estructura = None
    if transcripcion.strip():
        T, S = _T(), _S
        esquema = S(type=T.OBJECT,
                    required=["bloques", "palabras_totales", "duracion_estimada_seg",
                              "ritmo", "estilo_gancho"],
                    properties={
                        "bloques": S(type=T.INTEGER),
                        "palabras_totales": S(type=T.INTEGER),
                        "duracion_estimada_seg": S(type=T.INTEGER),
                        "ritmo": S(type=T.STRING),
                        "estilo_gancho": S(type=T.STRING),
                    })
        instruccion = f"""Analiza la ESTRUCTURA de esta transcripcion de un video de
YouTube. IMPORTANTE: no resumas ni repitas su contenido en tu respuesta,
solo estos datos numericos y descriptivos de FORMATO:

- bloques: en cuantos tramos/beats de contenido distintos se divide (cada
  cambio de sub-tema o "y ademas..." cuenta como uno)
- palabras_totales: cuenta total de palabras de la transcripcion
- duracion_estimada_seg: estimacion de segundos de narracion (a ~190
  palabras por minuto, la velocidad real de esta app)
- ritmo: una frase describiendo el ritmo (frases cortas y rapidas, pausado,
  con muchos datos numericos, etc) -- sin citar texto literal
- estilo_gancho: una frase describiendo COMO empieza (sin citarlo literal),
  ej. "pregunta directa al espectador", "afirmacion sorprendente"

TRANSCRIPCION (solo para este analisis, no la repitas):
{transcripcion[:8000]}"""
        estructura = _json(instruccion, esquema, log, "analizar_referencia")
        if estructura:
            log(f"Estructura detectada: {estructura['bloques']} bloques, "
                f"~{estructura['duracion_estimada_seg']}s, ritmo: {estructura['ritmo']}")
        else:
            log("AVISO: no se pudo analizar la estructura de la transcripcion.")
    return {"meta": meta, "estructura": estructura}


def proponer_ideas_similares(analisis, log=print):
    """Propone 3 ideas de video NUEVAS (tema DISTINTO al del video de
    referencia) que encajen en la misma estructura/formato detectado. Nunca
    usa el contenido real de la transcripcion, solo los metadatos de
    formato (numero de bloques, duracion, ritmo) ya extraidos antes."""
    meta = analisis.get("meta") or {}
    estructura = analisis.get("estructura") or {}
    T, S = _T(), _S
    esquema = S(type=T.OBJECT, required=["ideas"], properties={
        "ideas": S(type=T.ARRAY, items=S(
            type=T.OBJECT, required=["titulo", "resumen"],
            properties={"titulo": S(type=T.STRING), "resumen": S(type=T.STRING)}))
    })
    if estructura:
        pistas = (f"Estructura objetivo: {estructura.get('bloques', 6)} bloques de "
                  f"contenido, duracion total ~{estructura.get('duracion_estimada_seg', 300)} "
                  f"segundos, ritmo: {estructura.get('ritmo', 'normal')}, gancho inicial "
                  f"tipo: {estructura.get('estilo_gancho', 'pregunta directa')}.")
    else:
        pistas = "Estructura objetivo: 6-8 bloques, unos 5 minutos, ritmo documental."
    idioma_es = _es_espanol(meta.get("titulo", ""))
    aviso_idioma = (
        "IMPORTANTE: escribe el titulo y el resumen de las 3 ideas en ESPAÑOL."
        if idioma_es else
        "IMPORTANT: the reference video's title is in ENGLISH -- write the "
        "titulo and resumen of all 3 ideas in ENGLISH too, not Spanish, "
        "since that's the language of the audience/channel this is for.")
    instruccion = f"""{aviso_idioma}

Un video titulado "{meta.get('titulo','')}" (canal {meta.get('canal','')})
funciona bien en su nicho. {pistas}

Propon 3 ideas de video NUEVAS que encajen en exactamente esa misma
estructura y duracion, y que se queden en el MISMO nicho/tema general que el
titulo de arriba (si es sobre comportamiento animal, las 3 ideas nuevas
tienen que ser sobre comportamiento animal tambien; si es sobre cocina,
sobre cocina; etc.) -- el espectador que vio el video de referencia tiene
que sentir "esto es del mismo estilo", no "esto no tiene nada que ver".
Lo unico que cambia es el sujeto CONCRETO dentro de ese nicho (otro animal,
otro plato, otro fenomeno concreto), nunca el nicho entero.

UNA REGLA QUE NO SE PUEDE SALTAR, solo si aplica: si el sujeto concreto del
titulo de arriba ES en si mismo una obra con derechos de autor identificable
por su nombre real (una cancion, videoclip, pelicula, serie, videojuego,
libro concretos), entonces ninguna idea nueva puede girar en torno a OTRA
obra con nombre propio -- en ese caso (y SOLO en ese caso) cambia a un
fenomeno o categoria general dentro del mismo nicho en vez de otra obra
concreta. Esto no aplica a temas de naturaleza/ciencia/hechos generales
(animales, objetos, fenomenos): ahi SI se puede y se debe repetir el mismo
nicho exacto con otro sujeto concreto.

Cada idea con un titulo-gancho y un resumen de 2-3 frases de que trataria --
contenido totalmente original, nunca una version ni una continuacion literal
del video de referencia."""
    r = _json(instruccion, esquema, log, "ideas_similares")
    if not r:
        return []
    # se borran las propuestas del analisis anterior antes de añadir las
    # nuevas: si no, la lista se va llenando de tandas de referencias
    # distintas mezcladas sin distincion, y parece que un mismo analisis dio
    # resultados sin sentido cuando en realidad eran de otro video anterior
    ETIQUETA = "generada a partir de una referencia de formato"
    _guardar_ideas([i for i in listar_ideas() if i.get("notas") != ETIQUETA])
    ideas = []
    for it in r.get("ideas", []):
        idea = anadir_idea(it["titulo"], it["resumen"], notas=ETIQUETA, estructura=estructura)
        ideas.append(idea)
    log(f"{len(ideas)} ideas propuestas")
    return ideas


def escribir_guion_desde_idea(idea, log=print):
    """Escribe el GUION COMPLETO (texto narrado de principio a fin, no un
    resumen) a partir de una idea, ajustandose al objetivo de duracion/ritmo
    de la referencia de formato si la idea lo trae (ver analizar_video_referencia).
    Sin esto, "usar una idea" solo pegaba el titulo+resumen corto en la caja
    de guion, que no llega ni a un minuto de narracion -- esta funcion es la
    que de verdad genera el guion largo, en el mismo estilo que ya usa esta
    app (gancho directo, un aviso interno tipo "somewhere in that:" de vez en
    cuando, cierre con tesis, CTA final de suscribirse)."""
    T, S = _T(), _S
    esquema = S(type=T.OBJECT, required=["guion"], properties={"guion": S(type=T.STRING)})
    estructura = idea.get("estructura") or {}
    PPM = 190  # palabras por minuto reales de esta voz, calibrado con videos ya publicados
    if estructura.get("duracion_estimada_seg"):
        objetivo_palabras = round(estructura["duracion_estimada_seg"] / 60 * PPM)
        pistas = (f"Objetivo: aproximadamente {objetivo_palabras} palabras (para que dure "
                  f"~{estructura['duracion_estimada_seg']//60}:{estructura['duracion_estimada_seg']%60:02d} "
                  f"a {PPM} palabras/minuto), organizado en unos {estructura.get('bloques', 7)} "
                  f"bloques/momentos distintos, con un ritmo: {estructura.get('ritmo', 'documental, pausado')}. "
                  f"El gancho inicial debe ser del tipo: {estructura.get('estilo_gancho', 'pregunta directa al espectador')}.")
    else:
        pistas = "Objetivo: 950-1050 palabras (~5-6 minutos a 190 palabras/minuto), 7-9 bloques."
    idioma_es = _es_espanol(idea.get("titulo", "") + " " + idea.get("resumen", ""))
    aviso_idioma = "Escribe el guion en ESPAÑOL." if idioma_es else \
        "Write the script in ENGLISH (the idea below is in English)."
    instruccion = f"""{aviso_idioma}

Escribe el GUION COMPLETO de un video corto de divulgacion, listo para
narrar de principio a fin -- no un resumen ni un esquema, el texto real que
se va a leer en voz alta. {pistas}

Estilo (mismo que ya usa este canal): empieza con un gancho directo dirigido
al espectador, presenta cada hecho/comportamiento con una transicion natural
("Then there's...", "Start with...", etc. o su equivalente en español),
apoya cada afirmacion fuerte en un estudio o dato concreto y verificable
citando de forma natural (año, universidad o investigador si aplica, sin
inventar cifras), usa de vez en cuando un inciso corto tipo "Somewhere in
that:" seguido de un pensamiento breve seguido de un remate, y termina con
una frase de cierre tipo tesis y esta llamada a la accion exacta al final:
"If you enjoyed this, subscribe. I post one of these every week." (o su
equivalente natural en español si el guion es en español).

IDEA A DESARROLLAR:
Titulo: {idea.get('titulo','')}
Resumen: {idea.get('resumen','')}

Contenido totalmente original sobre este tema -- no menciones ni resumas
ningun video de referencia, escribe el guion como si el tema fuera tuyo desde
cero."""
    r = _json(instruccion, esquema, log, "escribir_guion_desde_idea")
    if not r:
        log("AVISO: no se pudo escribir el guion, intentalo de nuevo.")
        return None
    guion = r.get("guion", "").strip()
    log(f"Guion escrito: {len(guion.split())} palabras")
    return guion


def _iniciar():
    """Al importar: reabre el ultimo proyecto activo, o crea uno vacio si es la
    primera vez que se usa la app."""
    activar_proyecto(proyecto_activo() or "sin-titulo")


_iniciar()


def archivos_ref():
    """Las imagenes de estilo que se adjuntan en cada generacion: las
    compartidas por todos los videos (REFS) mas las propias de ESTE proyecto
    (personajes/animales que no pintan nada en otro video)."""
    archivos = set()
    for carpeta in (REFS, REF_PROYECTO):
        if carpeta and carpeta.is_dir():
            for patron in EXT_REF:
                archivos.update(carpeta.glob(patron))
    return sorted(archivos)

# ------------------------------------------------------------------ estilo
ESTILO = (
    "Crude hand-drawn doodle illustration, thick uneven black marker outlines, "
    "flat solid fills, no shading, no gradients, no texture. "
    "Humans are stick figures: round white head, two dot eyes, thin straight limbs. "
    "Animals are simple rounded cartoon shapes with the same thick outline. "
    "Background is minimal, always just one or two flat blocks of color and at "
    "most one or two simple props, nothing busy: but the SETTING itself must "
    "match this specific story, built from its fixed elements and its scene "
    "text. An outdoor nature story gets a flat sky band and ground band with a "
    "simple bush. An indoor domestic story gets a flat wall and floor band with "
    "at most one piece of furniture, never a sky, a horizon or outdoor foliage. "
    "Keep the same kind of setting (indoor or outdoor) consistently across the "
    "whole video, unless a compas explicitly moves the story elsewhere. "
    "Flat solid colors only, no gradients, outlines always #111111. "
    "16:9. Deliberately amateurish but clean."
)
TEXTO_ON = (
    "Render this exact text inside the image, spelled character for character: \"{t}\". "
    "Hand-lettered bold marker capitals, chunky rounded sans-serif in the same black "
    "#111111 as the outlines, slightly irregular like written with a thick felt pen. "
    "Very large: the text occupies roughly a third of the frame width. "
    "Placed in empty space, usually upper-left or lower-left, never overlapping the "
    "main subject. No speech bubbles, no boxes, no other words anywhere in the image."
)
TEXTO_OFF = "No text, letters or numbers anywhere in the image."
TEXTO_BOCADILLO = (
    "Draw a simple rounded comic-style thought bubble (a puffy cloud outline with "
    "2-3 small trailing circles connecting it down to the character's head), placed "
    "in empty space beside or above the head, never covering the face. Inside the "
    "bubble, in small black rounded hand-lettering, sentence case (NOT all-caps), "
    "write this exact thought, spelled character for character: \"{t}\". "
    "No other text, boxes or speech bubbles anywhere else in the image."
)
TEXTO_BOCADILLO_DOBLE = (
    "Both characters are thinking AT THE SAME TIME, each with their own separate "
    "comic-style thought bubble (a puffy cloud outline with 2-3 small trailing "
    "circles connecting down to that character's own head), one bubble per "
    "character, placed in empty space near each of their heads, never overlapping "
    "each other or covering either face. In small black rounded hand-lettering, "
    "sentence case (NOT all-caps): the first character's bubble reads, spelled "
    "character for character: \"{t}\" — the second character's bubble reads, "
    "spelled character for character: \"{t2}\". "
    "No other text, boxes or speech bubbles anywhere else in the image."
)


def _texto_pensamiento(rotulo, rotulo2):
    if rotulo2:
        return TEXTO_BOCADILLO_DOBLE.format(t=rotulo, t2=rotulo2)
    return TEXTO_BOCADILLO.format(t=rotulo)

TIPOS_PLANO = ["escena", "concepto", "detalle", "recuento", "diagrama", "rotulo", "pensamiento"]
PLANOS = """Every plano type below shares the SAME flat background treatment: one
solid color FROM THE FIXED PALETTE (never plain white, never a gradient, never
a new color outside the palette), so a "concepto" or "rotulo" shot still looks
like it belongs to the same video as a full "escena" -- only the composition
(what's on screen and how close) changes between types, never the background
language or rendering quality.
- escena: the story's real setting (indoor room or outdoor place, whichever
  fits this story), figures acting. Narration of events.
- concepto: a flat background in one palette color, one stick figure alone, no
  scenery. An abstract idea, a doubt, a question.
- detalle: a close-up of whatever matters most right now. Most "detalle" shots
  get NO circle at all: just a clean close-up. Add a thin yellow circle around
  the exact spot ONLY for the single most important piece of hard proof in
  that stretch of the video (a measurement, a mechanism, a specific physical
  flaw) -- it has to feel like a rare, deliberate highlight, never a habit.
  Circling a plain emotional or behavioral close-up (a face, a reaction, a
  gesture) looks like a random mistake, not a clue: never do that.
- recuento: an ABSTRACT SYMBOL repeated 2 to 5 times (calendar pages, bars,
  dots, paw prints, clocks, checkmarks...), on a flat palette background. A
  quantity or a rhythm. NEVER the protagonist or any established character
  drawn more than once in this shot -- asking the model to redraw the same
  character several times in one image is exactly what causes it to drift
  (different pose, different proportions, different shade each time), and a
  grid of the character's own face/body repeated reads as an absurd meme, not
  a serious quantity device. If the moment truly needs to show the character
  several times, use "escena" instead (one real scene, the repetition told
  through the scene itself, e.g. a calendar on the wall plus one instance of
  the character) rather than a grid.
- diagrama: thick black arrows over the object showing a force or a mechanism,
  on a flat palette background. Keep the object recognizable and proportioned
  normally -- never distort or enlarge it into something surreal just to make
  room for the arrows.
- rotulo: a flat palette background with one small element and a lot of free
  space (not literally empty/blank -- still a real color from the palette).
- pensamiento: a character (human or animal) with a comic-style thought bubble
  above its head holding a short thought or question, in its own natural voice.
  Optionally TWO characters in the same shot each thinking at once, one bubble
  each, when the joke is precisely the contrast between what each one thinks
  (e.g. the human thinks one thing about the animal while the animal thinks
  something completely different about the human, at the same time)."""

FUNCIONES = ["anomalia", "escalada", "contexto", "error",
             "revelacion", "factura", "tesis", "cta"]
FUNCION_PLANO = """Que plano pide cada funcion narrativa:
- anomalia   -> escena o detalle. Enseña el hecho fisico.
- escalada   -> recuento o escena. Repeticion, acumulacion.
- contexto   -> escena amplia. El mundo antes del problema.
- error      -> concepto o diagrama. Alguien se equivoca, alguien busca mal.
- revelacion -> detalle o diagrama. La prueba, el mecanismo.
- factura    -> recuento o escena. Las consecuencias, las cifras.
- tesis      -> concepto o rotulo. Fondo blanco, idea desnuda.
- cta        -> rotulo."""

MODELO_TEXTO = "gemini-3.8-flash"
# Imagenes: solo capa de pago. Coste por imagen a 1K, aprox:
#   gemini-3.1-flash-lite-image  ~0,034 $   <- suficiente para este estilo
#   gemini-3.1-flash-image       ~0,067 $
#   gemini-3-pro-image           ~0,134 $   <- para los planos clave
MODELO_IMG = "gemini-3.1-flash-lite-image"
# Se usa para corregir un plano concreto que YA fallo con el modelo barato:
# reintentar con el mismo modelo tiende a fallar igual (visto en la practica:
# 21/21 seguian mal tras reintentar con el barato), asi que la correccion
# usa un modelo mas capaz en vez de gastar otra vez en el mismo que fallo.
MODELO_IMG_RETRY = "gemini-3.1-flash-image"

# Coste aproximado por imagen, en euros, segun el modelo (para el contador de la web)
COSTE_IMG = {"gemini-3.1-flash-lite-image": 0.031,
             "gemini-3.1-flash-image": 0.062,
             "gemini-3-pro-image": 0.124}

# ElevenLabs, tarifa pay-as-you-go de eleven_multilingual_v2: 0,10 $ / 1000 caracteres
# (sept. 2026). Aproximado a euros; el motor "edge" es gratis y no se registra aqui.
COSTE_VOZ_1000 = 0.092

PPM = 155          # palabras por minuto de la locucion
OBJ_PALABRAS = 12  # objetivo por compas: se pasa del tope a proposito, para
                   # que el corte forzoso (_limitar_duracion) sea el que
                   # manda y la mayoria de planos queden cerca de MAX_SEG,
                   # no muy por debajo (media real observada antes: 2.0s
                   # con MAX_SEG=3.0 y este mismo objetivo en 7)
MIN_PALABRAS = 5   # por debajo de esto, se pega al compas anterior
LOTE = 20          # compases por llamada al escribir prompts

MAX_SEG = 3.0                          # ninguna imagen dura mas de esto
MAX_PALABRAS = int(MAX_SEG * PPM / 60)  # ~10 palabras

_cli = None
_PARADA = {"v": False}


# -------------------------------------------------------------------- gasto
def cargar_gasto():
    """Gasto real acumulado en este proyecto (imagenes + voz: el grueso del
    coste). Las llamadas de texto (prompts, rotulos, SEO...) son centimos y
    no se registran una a una."""
    if GASTO.exists():
        return json.loads(GASTO.read_text(encoding="utf-8"))
    return {"movimientos": [], "total": 0.0}


def _registrar_gasto(concepto, eur):
    if eur <= 0:
        return
    datos = cargar_gasto()
    datos["movimientos"].append({"cuando": time.strftime("%Y-%m-%d %H:%M:%S"),
                                 "concepto": concepto, "eur": round(eur, 4)})
    datos["movimientos"] = datos["movimientos"][-500:]      # no crecer sin limite
    datos["total"] = round(datos.get("total", 0.0) + eur, 4)
    OUT.mkdir(exist_ok=True)
    GASTO.write_text(json.dumps(datos, ensure_ascii=False, indent=2), encoding="utf-8")


def gasto_global():
    """Gasto real sumado de TODOS los proyectos guardados, no solo el activo."""
    total, por_proyecto = 0.0, []
    if PROYECTOS.is_dir():
        for p in PROYECTOS.iterdir():
            if not p.is_dir():
                continue
            g = p / "gasto.json"
            t = json.loads(g.read_text(encoding="utf-8")).get("total", 0.0) if g.exists() else 0.0
            if t:
                por_proyecto.append({"proyecto": p.name, "eur": round(t, 4)})
            total += t
    por_proyecto.sort(key=lambda x: -x["eur"])
    return {"total": round(total, 4), "por_proyecto": por_proyecto}


PRESUPUESTO = PROYECTOS / "presupuesto.json"


def limite_presupuesto():
    """Techo de gasto TOTAL de la app que tu mismo defines (no existe forma de
    consultar el saldo real de una cuenta de Gemini, asi que esto hace de red
    de seguridad en su lugar). None si no has puesto ninguno."""
    if PRESUPUESTO.exists():
        return json.loads(PRESUPUESTO.read_text(encoding="utf-8")).get("limite_eur")
    return None


def fijar_presupuesto(eur):
    """eur=None quita el limite (vuelve a 'sin limite')."""
    PROYECTOS.mkdir(exist_ok=True)
    if eur is None:
        PRESUPUESTO.unlink(missing_ok=True)
    else:
        PRESUPUESTO.write_text(json.dumps({"limite_eur": eur}, ensure_ascii=False), encoding="utf-8")


def consultar_elevenlabs():
    """Caracteres restantes en el plan de ElevenLabs. Si la API key es de solo
    lectura restringida (sin permiso 'user_read'), avisa en vez de fallar."""
    import requests
    try:
        r = requests.get("https://api.elevenlabs.io/v1/user/subscription",
                          headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"]}, timeout=15)
        if r.status_code == 401:
            return {"disponible": False,
                    "motivo": "la API key no tiene permiso para consultar la suscripcion "
                              "(activa 'user_read' en tu dashboard de ElevenLabs si quieres "
                              "este chequeo)"}
        r.raise_for_status()
        d = r.json()
        usados, limite = d.get("character_count", 0), d.get("character_limit", 0)
        return {"disponible": True, "usados": usados, "limite": limite,
                "restantes": max(0, limite - usados)}
    except Exception as ex:
        return {"disponible": False, "motivo": f"{type(ex).__name__}: {ex}"}


def comprobar_presupuesto(escenas=None):
    """Todo lo necesario para saber si puedes generar este video de principio a
    fin sin quedarte a medias: caracteres de ElevenLabs, y el gasto total de la
    app frente al limite que te hayas puesto. No cuesta nada: la consulta a
    ElevenLabs es solo metadatos, no genera audio."""
    escenas = escenas or cargar_escenas()
    frases = _frases_unicas(escenas)

    faltan_img = [e for e in escenas if not ruta_img(e["id"]).exists()]
    faltan_voz = [(f, t) for f, t in frases if not ruta_audio(f).exists()]

    coste_img = len(faltan_img) * COSTE_IMG.get(MODELO_IMG, 0)
    caracteres_voz = sum(len(t) for _, t in faltan_voz)
    coste_voz = caracteres_voz / 1000 * COSTE_VOZ_1000

    eleven = consultar_elevenlabs()
    globales = gasto_global()
    limite = limite_presupuesto()
    proyectado = round(globales["total"] + coste_img + coste_voz, 4)

    avisos = []
    if eleven.get("disponible") and caracteres_voz > eleven["restantes"]:
        avisos.append(f"ElevenLabs: hacen falta {caracteres_voz} caracteres y solo quedan "
                       f"{eleven['restantes']}. Te quedarias a medias generando la voz.")
    if limite is not None and proyectado > limite:
        avisos.append(f"Esto dejaria el gasto total de la app en {proyectado:.2f} €, "
                       f"por encima de tu limite de {limite:.2f} €.")

    return {
        "planos_pendientes": len(faltan_img),
        "coste_imagenes_pendiente": round(coste_img, 4),
        "caracteres_voz_pendiente": caracteres_voz,
        "coste_voz_pendiente": round(coste_voz, 4),
        "coste_total_pendiente": round(coste_img + coste_voz, 4),
        "elevenlabs": eleven,
        "gasto_app_total": globales["total"],
        "gasto_por_proyecto": globales["por_proyecto"],
        "gasto_app_proyectado": proyectado,
        "limite_eur": limite,
        "avisos": avisos,
        "ok": not avisos,
    }


def parar(valor=True):
    """Marca que el trabajo en curso debe detenerse tras el elemento actual."""
    _PARADA["v"] = bool(valor)


def parado():
    return _PARADA["v"]


def cliente():
    global _cli
    if _cli is None:
        from google import genai
        _cli = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    return _cli


# ------------------------------------------------------------------ estado
def cargar_escenas():
    if not ESCENAS.exists():
        return []
    return json.loads(ESCENAS.read_text(encoding="utf-8"))


def guardar_escenas(escenas):
    OUT.mkdir(exist_ok=True)
    ESCENAS.write_text(json.dumps(escenas, ensure_ascii=False, indent=2), encoding="utf-8")


def ruta_img(i):
    return IMGS / f"{i:03d}.png"


def ruta_audio(frase_id):
    """El audio es por FRASE, no por compas: varios planos comparten mp3."""
    return AUDIO / f"f{frase_id:03d}.mp3"


# ----------------------------------------------------------------- compases
def partir_frases(texto):
    """Corte por frase. Unidad de AUDIO."""
    texto = re.sub(r"\s+", " ", texto).strip()
    bruto = re.split(r"(?<=[.!?…])\s+(?=[¿¡\"'«A-ZÁÉÍÓÚÑ0-9])", texto)
    frases, buffer = [], ""
    for f in bruto:
        f = f.strip()
        if not f:
            continue
        buffer = (buffer + " " + f).strip() if buffer else f
        if len(buffer.split()) >= 4:
            frases.append(buffer)
            buffer = ""
    if buffer:
        if frases:
            frases[-1] += " " + buffer
        else:
            frases.append(buffer)
    return frases


_CONECT = (r"(?:y|pero|porque|aunque|mientras|cuando|para|que|si|sino|ni|como|donde|hasta|desde|sin|con"
           r"|and|but|because|although|while|when|for|that|if|unless|nor|as|where|until|since|without|so|yet)")


def _puntos_corte(frase):
    """Sitios donde se puede cambiar de dibujo sin romper la locucion."""
    pos = set()
    for m in re.finditer(r"[,;:]\s+", frase):
        pos.add(m.end())
    for m in re.finditer(rf"\s+(?={_CONECT}\b)", frase):
        pos.add(m.end())
    return sorted(pos)


def _limitar_duracion(trozos):
    """Ningun compas puede superar MAX_SEG: si uno se pasa, se corte por
    palabras a partes iguales (no hace falta pausa natural, la locucion de
    la frase sigue sonando de corrido, solo cambia el dibujo)."""
    resultado = []
    for t in trozos:
        palabras = t.split()
        if len(palabras) <= MAX_PALABRAS:
            resultado.append(t)
            continue
        n = -(-len(palabras) // MAX_PALABRAS)   # partes necesarias
        tam = -(-len(palabras) // n)            # tamano por parte, repartido
        for i in range(0, len(palabras), tam):
            resultado.append(" ".join(palabras[i:i + tam]))
    return resultado


def partir_compases(frase):
    """Una frase -> uno o varios compases EQUILIBRADOS. Unidad de IMAGEN."""
    pal = frase.split()
    if len(pal) <= OBJ_PALABRAS + 2:
        return _limitar_duracion([frase])
    cortes = _puntos_corte(frase)
    if not cortes:
        return _limitar_duracion([frase])

    # cuantos trozos quiero, y donde caerian si fueran perfectos
    n = max(2, round(len(pal) / OBJ_PALABRAS))
    info = [(c, len(frase[:c].split())) for c in cortes]
    elegidos = []
    for i in range(1, n):
        objetivo = len(pal) * i / n
        c, _ = min(info, key=lambda t: abs(t[1] - objetivo))
        if c not in elegidos:
            elegidos.append(c)
    elegidos.sort()

    trozos, ini = [], 0
    for c in elegidos:
        t = frase[ini:c].strip()
        if t:
            trozos.append(t)
            ini = c
    resto = frase[ini:].strip()
    if resto:
        trozos.append(resto)

    fusionados = []
    for t in trozos:
        if fusionados and len(t.split()) < MIN_PALABRAS:
            fusionados[-1] = (fusionados[-1] + " " + t).strip()
        else:
            fusionados.append(t)
    # si el primero se quedo corto, se pega al siguiente
    while len(fusionados) > 1 and len(fusionados[0].split()) < MIN_PALABRAS:
        fusionados[1] = (fusionados[0] + " " + fusionados[1]).strip()
        fusionados.pop(0)
    return _limitar_duracion(fusionados) or [frase]


def desglosar(texto_guion):
    """Devuelve [{id, frase_id, texto, texto_frase}] listo para rellenar."""
    escenas, cid = [], 0
    for fid, frase in enumerate(partir_frases(texto_guion), 1):
        for compas in partir_compases(frase):
            cid += 1
            escenas.append({"id": cid, "frase_id": fid,
                            "texto": compas, "texto_frase": frase})
    return escenas


def duracion_estimada(escena):
    return len(escena["texto"].split()) / PPM * 60


# -------------------------------------------------------------- llamadas IA
def _json(instruccion, esquema, log, etiqueta=""):
    from google.genai import types
    for intento in range(3):
        try:
            r = cliente().models.generate_content(
                model=MODELO_TEXTO,
                contents=instruccion,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=esquema,
                ),
            )
            return json.loads(r.text)
        except Exception as ex:
            log(f"  {etiqueta} fallo (intento {intento+1}): {type(ex).__name__}: {ex}")
            time.sleep(3)
    return None


def _S(**kw):
    from google.genai import types
    return types.Schema(**kw)


def _T():
    from google.genai import types
    return types.Type


# 1) biblia visual -----------------------------------------------------------
def biblia_visual(texto_guion, anterior=None, log=print):
    T = _T()
    esquema = _S(type=T.OBJECT, required=["elementos", "paleta"], properties={
        "elementos": _S(type=T.ARRAY, items=_S(
            type=T.OBJECT, required=["nombre", "descripcion"],
            properties={"nombre": _S(type=T.STRING), "descripcion": _S(type=T.STRING)})),
        "paleta": _S(type=T.ARRAY, items=_S(type=T.STRING)),
    })
    nombres_antes = [e["nombre"] for e in (anterior or {}).get("elementos", [])]
    aviso_nombres = (f"""
YA EXISTE una biblia visual de una version anterior de este mismo guion, con
estos nombres de elemento: {', '.join(nombres_antes)}. Si el elemento sigue
apareciendo, REUTILIZA su nombre EXACTO (ni una tilde distinta): cada nombre
nuevo obliga a regenerar su hoja de referencia desde cero, tirando la
anterior y gastando de nuevo. Solo usa un nombre distinto si es un elemento
que de verdad no existia antes.
""" if nombres_antes else "")
    instruccion = f"""Lee este guion de video y prepara su BIBLIA VISUAL, para que el
mismo dibujo salga identico en decenas de imagenes generadas por separado (cada
imagen la genera un modelo distinto, sin memoria de las demas, asi que sin esta
biblia cada una reinterpreta todo desde cero y no hay dos iguales).
{aviso_nombres}
1) "elementos": personajes, animales y objetos RECURRENTES (aparecen varias
   veces). Maximo 8. Para cada uno, una descripcion en INGLES, corta y muy
   concreta: forma, color EXACTO PLANO, un rasgo distintivo. Esa descripcion
   se copiara literal en todos los prompts donde aparezca el elemento, asi
   que debe ser siempre la misma. No incluyas el fondo ni el estilo general,
   solo el elemento en si. Ejemplo: "stick figure, round white head, olive
   green backpack, small notebook".
   PROHIBIDO describir brillo, iridiscencia, textura, pelaje/plumaje
   detallado, sombreado o reflejos (glossy, shiny, iridescent, fluffy,
   textured, metallic sheen...) por muy realista que sea el animal/objeto de
   verdad: el dibujo final es plano sin sombras, y una descripcion asi hace
   que la propia hoja de referencia salga con degradados, contradiciendo el
   estilo desde el principio y arruinando la consistencia de todo el video.
   Usa solo el nombre plano del color ("dark navy blue", "matte black"),
   nunca su acabado o brillo.

2) "paleta": 4-5 colores fijos en ingles (nombre + donde se usa), pensados
   para el entorno REAL de esta historia (una casa, una calle, una oficina,
   la naturaleza... lo que corresponda segun el guion). Estos colores se
   repiten en TODAS las imagenes del video, asi que tienen que tener sentido
   para ese entorno concreto, no ser un capricho. Ejemplo para una historia
   domestica: ["warm cream walls", "dusty blue couch", "honey-tan wood
   floor", "sage green plant leaves", "black outlines"].

GUION:
{texto_guion[:6000]}"""
    r = _json(instruccion, esquema, log, "biblia")
    if not r:
        log("AVISO: sin biblia visual, los elementos y colores variaran entre planos.")
        return {"elementos": [], "paleta": []}
    log(f"Biblia visual: {', '.join(x['nombre'] for x in r.get('elementos', []))}")
    log(f"Paleta: {', '.join(r.get('paleta', []))}")
    return r


def cargar_biblia():
    p = OUT / "biblia.json"
    if not p.exists():
        return {"elementos": [], "paleta": []}
    return json.loads(p.read_text(encoding="utf-8"))


# 2) funciones narrativas ----------------------------------------------------
def marcar_funciones(escenas, log=print):
    T = _T()
    esquema = _S(type=T.ARRAY, items=_S(
        type=T.OBJECT, required=["inicio", "fin", "funcion"],
        properties={"inicio": _S(type=T.INTEGER), "fin": _S(type=T.INTEGER),
                    "funcion": _S(type=T.STRING, enum=FUNCIONES)}))
    lineas = "\n".join(f"{e['id']}. {e['texto']}" for e in escenas)
    instruccion = f"""Te paso un guion numerado por compases. Divide el video en TRAMOS
consecutivos y etiqueta cada tramo con su funcion narrativa.

Funciones posibles: {', '.join(FUNCIONES)}
- anomalia: se presenta el hecho raro
- escalada: se repite y crece
- contexto: como funcionaba el mundo antes
- error: alguien busca mal, decide mal o cree haberlo resuelto
- revelacion: la causa real, el mecanismo
- factura: las consecuencias y las cifras
- tesis: la conclusion universal
- cta: llamada a suscribirse

Cubre TODOS los compases del 1 al {len(escenas)}, sin huecos ni solapes.
Tramos de entre 5 y 30 compases.

COMPASES:
{lineas}"""
    tramos = _json(instruccion, esquema, log, "funciones")
    por_id = {}
    for t in (tramos or []):
        for i in range(int(t["inicio"]), int(t["fin"]) + 1):
            por_id[i] = t["funcion"]
    huecos = [e["id"] for e in escenas if e["id"] not in por_id]
    for e in escenas:
        e["funcion"] = por_id.get(e["id"], "contexto")
    if tramos:
        log(f"Estructura: {len(tramos)} tramos "
            f"({', '.join(t['funcion'] for t in tramos)})")
    if huecos:
        log(f"AVISO: {len(huecos)} compases sin funcion narrativa "
            f"(del {huecos[0]} al {huecos[-1]}), se marcan como 'contexto'")
    return escenas


# 3) prompts, en lotes con contexto rodante ----------------------------------
def _esquema_prompts():
    T = _T()
    return _S(type=T.ARRAY, items=_S(
        type=T.OBJECT, required=["id", "plano", "prompt"],
        properties={"id": _S(type=T.INTEGER),
                    "plano": _S(type=T.STRING, enum=TIPOS_PLANO),
                    "prompt": _S(type=T.STRING),
                    "pensamiento": _S(type=T.STRING),
                    "pensamiento2": _S(type=T.STRING)}))


def escribir_prompts(escenas, biblia, log=print):
    fijos = "\n".join(f'- {b["nombre"]}: "{b["descripcion"]}"'
                      for b in biblia.get("elementos", []))
    anteriores = []
    for ini in range(0, len(escenas), LOTE):
        trozo = escenas[ini:ini + LOTE]
        lineas = "\n".join(
            f'{e["id"]}. [{e.get("funcion","contexto")}] {e["texto"]}' for e in trozo)
        cola = "\n".join(f"- {p}" for p in anteriores[-3:]) or "(es el principio del video)"
        instruccion = f"""Escribe el prompt de imagen de cada compas de un video narrado
con dibujos de monigotes.

ELEMENTOS FIJOS (copia estas descripciones literalmente cuando el elemento aparezca):
{fijos or "(ninguno)"}

TIPOS DE PLANO:
{PLANOS}

{FUNCION_PLANO}

REGLAS:
- El corchete de cada linea es la funcion narrativa: usala para elegir el plano.
- El "prompt" va en INGLES, describe UNA imagen concreta, una o dos frases.
- Cuando dos compases seguidos hablan de lo mismo con un cambio (antes/despues,
  lleno/vacio, muchos/pocos), repite EXACTAMENTE la misma composicion cambiando solo
  lo que cambia. Ese eco visual es el mejor recurso del formato: usalo cuando toque.
- Fuera de esos ecos, evita tres planos seguidos del mismo tipo.
- El circulo amarillo de "detalle" es un recurso escaso: en este bloque de
  {len(trozo)} compases, uselo como mucho en 1 o 2, y solo si de verdad hay
  una prueba fisica concreta que señalar. Si dudas, no lo pongas.
- Da vida a la escena: cuando el compas lo permita, mete objetos y contexto
  concretos (un cuenco con su nombre, un sofa, una ventana con lluvia, otro
  personaje reaccionando) en vez de un personaje solo en fondo vacio. Usa
  "concepto" (fondo blanco, un monigote solo) solo para ideas de verdad
  abstractas, no como opcion por defecto.
- NUNCA inventes un escenario, animal o personaje nuevo que no este en los
  ELEMENTOS FIJOS, ni siquiera para ilustrar un dato general/biologico/de un
  estudio (una gata generica con crias en un granero, un antepasado salvaje
  en la naturaleza, un gato de una protectora). Eso rompe la consistencia
  visual del video entero. En su lugar, usa "concepto" o "diagrama": fondo
  blanco, una silueta simple en el MISMO estilo del video representando la
  idea general, nunca un mundo o personaje distinto con su propio escenario.
- Nada de texto dentro de la imagen: los rotulos se deciden despues, EXCEPTO
  en el plano "pensamiento" (ver mas abajo).
- Si eliges el plano "pensamiento", rellena tambien "pensamiento" con el
  pensamiento o pregunta corta, en el MISMO IDIOMA en que esta escrito el
  guion (no traduzcas), en su voz natural (minusculas normales, no en
  mayusculas), coherente con lo que cuenta este compas. IMPORTANTE: el plano
  dura en pantalla lo mismo que tarda en locutarse el texto de ese compas
  (la columna de la izquierda), asi que el pensamiento NUNCA puede tener mas
  palabras que ese texto hablado -- si es mas largo, no da tiempo a leerlo y
  se pierde. Si el compas es corto, el pensamiento tiene que ser corto
  tambien (2-4 palabras vale perfectamente). En cualquier otro plano, deja
  "pensamiento" vacio.
- Si en ese mismo plano de "pensamiento" hay DOS personajes a la vista (por
  ejemplo la persona y el animal) y el gancho del compas es precisamente el
  contraste entre lo que cada uno cree del otro, rellena tambien
  "pensamiento2" con el pensamiento del segundo personaje, mismas reglas de
  idioma y de longitud maxima (recuerda que ahora hay DOS bocadillos que leer
  en el mismo tiempo, asi que si acaso mas cortos todavia, no mas largos).
  Si solo hay un personaje pensando, deja "pensamiento2" vacio: no lo
  inventes por rellenar.
- Cuando dos personajes estan interactuando y lo interesante es lo que cada
  uno interpreta del otro (no solo el gesto fisico), prefiere el plano
  "pensamiento" antes que "escena": es el recurso mas fuerte del formato para
  enseñar el desfase de percepcion entre ellos, mejor que narrarlo por fuera.

ULTIMOS PLANOS DEL TRAMO ANTERIOR (para continuidad de escenario):
{cola}

COMPASES:
{lineas}"""
        r = _json(instruccion, _esquema_prompts(), log,
                  f"prompts {trozo[0]['id']}-{trozo[-1]['id']}")
        datos = {int(p["id"]): p for p in (r or [])}
        sin = []
        for e in trozo:
            d = datos.get(e["id"])
            if d:
                e["plano"] = d.get("plano", "escena")
                e["prompt"] = d.get("prompt", "")
                if e["plano"] == "pensamiento" and (d.get("pensamiento") or "").strip():
                    e["rotulo"] = d["pensamiento"].strip()
                    if (d.get("pensamiento2") or "").strip():
                        e["rotulo2"] = d["pensamiento2"].strip()
            if not d or not (e.get("prompt") or "").strip():
                e["plano"] = e.get("plano") or "escena"
                e["prompt"] = ""          # vacio, no la frase en espanol
                sin.append(e["id"])
        if sin:
            log(f"  AVISO: sin prompt los planos {sin}. Escribelos a mano o "
                f"vuelve a crear el storyboard.")
        anteriores += [e["prompt"] for e in trozo if e["prompt"]]
        log(f"  prompts {trozo[-1]['id']}/{len(escenas)}")
    return escenas


# 4) rotulos, repartidos mirando el conjunto ---------------------------------
# Palabras que pierden la eñe y cambian de significado. Se corrigen solas.
ENE = {"ANO": "AÑO", "ANOS": "AÑOS", "NINO": "NIÑO", "NINOS": "NIÑOS",
       "NINA": "NIÑA", "NINAS": "NIÑAS", "ESPANA": "ESPAÑA", "SENOR": "SEÑOR",
       "SENAL": "SEÑAL", "SENALES": "SEÑALES", "MANANA": "MAÑANA",
       "PEQUENO": "PEQUEÑO", "PEQUENA": "PEQUEÑA", "SUENO": "SUEÑO",
       "DANO": "DAÑO", "DANOS": "DAÑOS", "ENGANO": "ENGAÑO", "CANON": "CAÑON",
       "MONTANA": "MONTAÑA", "CAMPANA": "CAMPAÑA", "EXTRANO": "EXTRAÑO",
       "MANANAS": "MAÑANAS", "DUENO": "DUEÑO", "BANO": "BAÑO"}
TILDES = str.maketrans("ÁÉÍÓÚÀÈÌÒÙÂÊÎÔÛÄËÏÖÜ", "AEIOUAEIOUAEIOUAEIOU")


def _limpiar_rotulo(r):
    """Quita tildes, devuelve la eñe robada y normaliza."""
    r = (r or "").strip().upper()
    if not r:
        return ""
    r = " ".join(ENE.get(p, p) for p in r.split())
    return r.translate(TILDES).replace("¿", "").replace("¡", "")


def _es_eco(a, b):
    """Dos rotulos seguidos solo valen si comparten composicion (mismo tipo de plano)."""
    return a.get("plano") == b.get("plano") and a.get("plano") in ("recuento", "rotulo")


def repartir_rotulos(escenas, log=print):
    """Reparte los rotulos tipo cartel. Los planos "pensamiento" quedan fuera:
    ya trajeron su bocadillo puesto desde escribir_prompts()."""
    T = _T()
    esquema = _S(type=T.ARRAY, items=_S(
        type=T.OBJECT, required=["id", "rotulo"],
        properties={"id": _S(type=T.INTEGER), "rotulo": _S(type=T.STRING)}))
    BLOQUE = 40
    objetivo = [e for e in escenas if e.get("plano") != "pensamiento"]
    for ini in range(0, len(objetivo), BLOQUE):
        trozo = objetivo[ini:ini + BLOQUE]
        lineas = "\n".join(
            f'{e["id"]}. [{e.get("plano","escena")}] {e["texto"]}' for e in trozo)
        instruccion = """Decide el rotulo que va dibujado DENTRO de cada imagen.

REGLAS, por orden de prioridad:

1. NUNCA dos compases seguidos con rotulo, salvo que sean un eco (el segundo añade
   algo al primero: "UNO AL MES" y luego "CADA MES"). Esta regla manda sobre todas
   las demas. Antes de responder, recorre tu lista y comprueba que no hay dos
   seguidos; si los hay, vacia uno.
2. De los {n} compases de esta lista, entre {lo} y {hi} deben llevar rotulo, ni mas
   ni menos. Los demas van con cadena vacia. Cuentalos antes de responder.
3. En el MISMO IDIOMA en que estan escritos los compases de abajo. NO traduzcas:
   si los compases estan en ingles, el rotulo va en ingles; si estan en español,
   va en español; etc.
4. Si el idioma es español: la Ñ SE ESCRIBE SIEMPRE ("CINCO AÑOS", "NIÑOS",
   "MAÑANA", "ESPAÑA"; quitarle la eñe cambia el significado y es un error
   grave) y SIN TILDES en las vocales (nada de Á É Í Ó Ú, el generador las
   dibuja mal: "SEGUIA AHI", no "SEGUÍA AHÍ").
5. MAYUSCULAS, de una a tres palabras. Nunca una frase.
6. Es la idea clave del compas, no un resumen: "RINOCERONTES MUERTOS", "UNO AL MES",
   "1996". Una fecha o una cifra suelta es un rotulo excelente.
7. Prioriza los compases de mas impacto y los planos de tipo "rotulo" y "recuento".
8. No repitas el mismo rotulo en compases distintos.

COMPASES:
{lineas}""".format(n=len(trozo), lo=int(len(trozo)*.42), hi=int(len(trozo)*.55),
                   lineas=lineas)
        r = _json(instruccion, esquema, log,
                  f"rotulos {trozo[0]['id']}-{trozo[-1]['id']}")
        datos = {int(p["id"]): (p.get("rotulo") or "").strip().upper() for p in (r or [])}
        for e in trozo:
            e["rotulo"] = _limpiar_rotulo(datos.get(e["id"], ""))
        log(f"  rotulos {trozo[-1]['id']}/{len(objetivo)}")
    # red de seguridad: si aun asi quedan dos seguidos, se vacia el segundo
    # (los "pensamiento" no cuentan: es un recurso visual distinto)
    for a, b in zip(escenas, escenas[1:]):
        if (a.get("rotulo") and b.get("rotulo") and not _es_eco(a, b)
                and a.get("plano") != "pensamiento" and b.get("plano") != "pensamiento"):
            b["rotulo"] = ""
    con = sum(1 for e in escenas if e.get("rotulo"))
    log(f"Rotulos: {con} de {len(escenas)} planos ({round(100*con/max(1,len(escenas)))}%)")
    return escenas


# ------------------------------------------------------------- orquestacion
def generar_escenas(texto_guion, log=print):
    escenas = desglosar(texto_guion)
    nf = len({e["frase_id"] for e in escenas})
    dur = [duracion_estimada(e) for e in escenas]
    log(f"{nf} frases -> {len(escenas)} planos "
        f"(media {sum(dur)/len(dur):.1f} s, maximo {max(dur):.1f} s)")

    biblia_antes = cargar_biblia()
    biblia = biblia_visual(texto_guion, biblia_antes, log)
    marcar_funciones(escenas, log)
    escribir_prompts(escenas, biblia, log)
    repartir_rotulos(escenas, log)

    # si el reparto de compases cambio (ej. al bajar MAX_SEG), el mismo id
    # numerico puede apuntar ahora a un texto distinto: la imagen vieja de ese
    # id ya no corresponde a nada y hay que borrarla, no dejarla puesta como
    # si estuviera bien (eso fue el bug real de los planos 63-88/100/140).
    anteriores = cargar_escenas() if ESCENAS.exists() else []
    texto_antes = {e["id"]: e.get("texto") for e in anteriores}
    borradas = [e["id"] for e in escenas
                if texto_antes.get(e["id"]) not in (None, e["texto"])
                and ruta_img(e["id"]).exists()]
    for eid in borradas:
        ruta_img(eid).unlink()
    if borradas:
        log(f"  {len(borradas)} imagen(es) vieja(s) borradas por no corresponder "
            f"ya a su plano (reparto de compases distinto): {borradas}")

    OUT.mkdir(exist_ok=True)
    (OUT / "biblia.json").write_text(
        json.dumps(biblia, ensure_ascii=False, indent=2), encoding="utf-8")
    guardar_escenas(escenas)

    elementos = biblia.get("elementos", [])
    if elementos:
        # limpia hojas huerfanas de una biblia anterior (nombres que ya no
        # existen): si no, se acumulan duplicados y cada imagen del video
        # recibe cada vez mas referencias, algunas de un diseño ya descartado
        esperados = {f"{slug(e['nombre'], 3)}.jpeg" for e in elementos}
        if REF_PROYECTO.is_dir():
            for p in REF_PROYECTO.iterdir():
                if p.is_file() and p.name not in esperados:
                    p.unlink()
                    log(f"  hoja huerfana borrada: {p.name}")

        # YA NO se generan solas: cuestan dinero y, si la biblia describe
        # algo con brillo/textura sin querer, la referencia sale mal y
        # arrastra el error a las 100+ imagenes que la copian. Ahora se
        # avisa de lo que falta y lo sube la persona (panel "Referencias"),
        # imagen real elegida a proposito, no generada a ciegas.
        faltan = [e["nombre"] for e in elementos
                  if not (REF_PROYECTO / f"{slug(e['nombre'], 3)}.jpeg").exists()]
        if faltan:
            log(f"FALTAN {len(faltan)} imagen(es) de referencia antes de generar "
                f"nada mas -- sube una por cada uno en el panel 'Referencias': "
                f"{', '.join(faltan)}")
        else:
            log("Todas las hojas de referencia ya estan puestas, listo para generar.")

    log(f"Listo: {len(escenas)} planos en {ESCENAS}")
    return escenas


# ---------------------------------------------------------------- imagenes
_MIME_REF = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
             ".webp": "image/webp"}


def _refs():
    from google.genai import types
    return [types.Part.from_bytes(data=p.read_bytes(),
                                  mime_type=_MIME_REF.get(p.suffix.lower(), "image/png"))
            for p in archivos_ref()]


def _refs_de(escena, biblia=None):
    """Referencias para ESTE plano en concreto: las globales (estilo humano)
    siempre, y de las propias del proyecto solo las de los elementos que de
    verdad aparecen en su prompt -- adjuntar TODAS las hojas a TODOS los
    planos (lo de antes) diluye la atencion del modelo justo en el personaje
    que mas importa que salga igual. Si no se puede determinar (biblia
    vacia o ningun match), se cae a todas por seguridad."""
    from google.genai import types
    biblia = biblia if biblia is not None else cargar_biblia()
    elementos = biblia.get("elementos", [])
    archivos = set()
    if REFS.is_dir():
        for patron in EXT_REF:
            archivos.update(REFS.glob(patron))
    if REF_PROYECTO.is_dir():
        texto = f"{escena.get('prompt','')} {escena.get('texto','')}"
        esperados = set()
        alguno = False
        for elem in elementos:
            archivo = REF_PROYECTO / f"{slug(elem['nombre'], 3)}.jpeg"
            esperados.add(archivo.name)
            if archivo.exists():
                desc = (elem.get("descripcion") or "").strip()
                if desc and desc in texto:
                    archivos.add(archivo)
                    alguno = True
        if not alguno:
            # no se pudo emparejar ninguno (paráfrasis, biblia vacia...):
            # mejor pasar todas las del proyecto que quedarse sin ninguna
            for p in REF_PROYECTO.iterdir():
                if p.is_file():
                    archivos.add(p)
        else:
            # subidas a mano que no siguen el naming de la biblia: incluirlas
            # siempre, no hay forma de saber a que plano corresponden
            for p in REF_PROYECTO.iterdir():
                if p.is_file() and p.name not in esperados:
                    archivos.add(p)
    return [types.Part.from_bytes(data=p.read_bytes(),
                                  mime_type=_MIME_REF.get(p.suffix.lower(), "image/png"))
            for p in sorted(archivos)]


def prompt_de(escena):
    rotulo = (escena.get("rotulo") or "").strip()
    if rotulo and escena.get("plano") == "pensamiento":
        texto = _texto_pensamiento(rotulo, (escena.get("rotulo2") or "").strip())
    elif rotulo:
        texto = TEXTO_ON.format(t=rotulo)
    else:
        texto = TEXTO_OFF
    paleta = cargar_biblia().get("paleta") or []
    paleta_txt = (f"Palette: ONLY these colors, nothing else: {', '.join(paleta)}. "
                  if paleta else "")
    p = (f"Shot type: {escena.get('plano', 'escena')}.\n"
         f"{escena['prompt']}\n\n"
         f"Estilo: {ESTILO} {paleta_txt}\n\n{texto}")
    if archivos_ref():
        p += ("\n\nIMPORTANTE sobre las imagenes adjuntas: no son inspiracion ni "
              "un punto de partida, son la referencia OBLIGATORIA. Para cada "
              "personaje u objeto que reconozcas en ellas, COPIA su diseño tal "
              "cual: misma forma exacta, mismas proporciones, mismos colores, "
              "mismos detalles distintivos. NO lo rediseñes, NO lo redibujes 'a tu "
              "manera', NO cambies su forma ni sus colores aunque el texto de la "
              "escena no repita cada detalle -- ante cualquier duda, la imagen "
              "adjunta manda siempre sobre tu propia interpretacion. Solo la pose, "
              "el angulo y la expresion pueden cambiar segun lo que pida esta "
              "escena en concreto; el diseño del personaje en si, nunca.")
    return p


def _guardar_checks(escenas):
    """Guarda SOLO el resultado del control, sin pisar lo que hayas editado
    en la interfaz mientras se generaban las imagenes."""
    try:
        disco = cargar_escenas()
        if not disco:
            return
        checks = {e["id"]: e.get("check") for e in escenas if "check" in e}
        for e in disco:
            if e["id"] in checks:
                if checks[e["id"]]:
                    e["check"] = checks[e["id"]]
                else:
                    e.pop("check", None)
        guardar_escenas(disco)
    except Exception:
        pass


_PISTAS_ES = re.compile(
    r"[ñÑ¿¡áéíóúÁÉÍÓÚ]|\b(que|de|la|el|los|las|en|por|para|con|una|se|le|es|del|al|"
    r"aqui|esta|esto|asi|muy|pero|porque|tanto|mas)\b",
    re.I)


def _es_espanol(txt):
    return bool(_PISTAS_ES.search(txt or ""))


def comprobar(escenas):
    """Revision GRATIS del storyboard, antes de gastar un euro.
    Devuelve [{id, nivel, texto}] con nivel 'error' o 'aviso'."""
    problemas = []
    def p(e, nivel, txt):
        problemas.append({"id": e["id"], "nivel": nivel, "texto": txt})

    esperado = 1
    ant_rot = False
    for e in escenas:
        if e["id"] != esperado:
            p(e, "error", f"numeracion rota: esperaba {esperado}")
        esperado = e["id"] + 1

        prom = (e.get("prompt") or "").strip()
        if not prom:
            p(e, "error", "sin prompt: escribelo a mano antes de generar")
        elif prom == (e.get("texto") or "").strip():
            p(e, "error", "el prompt es la frase en espanol, no una descripcion visual")
        elif len(prom.split()) < 6:
            p(e, "aviso", "prompt demasiado corto, la imagen saldra generica")
        elif not re.search(r"\b(the|a|an|of|with|on|in|and)\b", prom, re.I):
            p(e, "aviso", "el prompt no parece estar en ingles")
        if e.get("plano") not in TIPOS_PLANO:
            p(e, "error", f"tipo de plano invalido: {e.get('plano')}")

        r = (e.get("rotulo") or "").strip()
        r2 = (e.get("rotulo2") or "").strip()
        guion_es = _es_espanol(e.get("texto"))
        if e.get("plano") == "pensamiento":
            palabras_compas = len((e.get("texto") or "").split())
            if not r:
                p(e, "aviso", "plano de pensamiento sin la frase del bocadillo")
            else:
                if len(r.split()) > 10:
                    p(e, "aviso", f"bocadillo de {len(r.split())} palabras, puede quedar apretado")
                if len(r.split()) > palabras_compas * 2:
                    p(e, "error", f"bocadillo de {len(r.split())} palabras pero el plano solo dura "
                                  f"lo que tarda en decirse '{e.get('texto')}' ({palabras_compas} "
                                  f"palabras): no da tiempo a leerlo")
                if _es_espanol(r) != guion_es:
                    p(e, "error", f"bocadillo en otro idioma que el guion: '{r}'")
            if r2:
                if len(r2.split()) > 10:
                    p(e, "aviso", f"segundo bocadillo de {len(r2.split())} palabras, puede quedar apretado")
                if len(r.split()) + len(r2.split()) > palabras_compas * 2:
                    p(e, "error", f"dos bocadillos ({len(r.split())}+{len(r2.split())} palabras) "
                                  f"para un plano que solo dura {palabras_compas} palabras habladas: "
                                  f"no da tiempo a leer los dos")
                if _es_espanol(r2) != guion_es:
                    p(e, "error", f"segundo bocadillo en otro idioma que el guion: '{r2}'")
        elif r:
            if r != r.upper():
                p(e, "aviso", "rotulo en minusculas")
            if len(r.split()) > 3:
                p(e, "aviso", f"rotulo de {len(r.split())} palabras, se dibujara mal")
            if _es_espanol(r) != guion_es:
                p(e, "error", f"rotulo en otro idioma que el guion: '{r}'")
            if any(c in r for c in "ÁÉÍÓÚÜáéíóúü¿¡"):
                p(e, "aviso", "rotulo con tildes o signos: el generador los dibuja mal")
            robadas = [w for w in r.split() if w in ENE]
            if robadas:
                p(e, "error", f"falta la eñe: '{robadas[0]}' deberia ser "
                              f"'{ENE[robadas[0]]}' y significa otra cosa")
            if ant_rot:
                p(e, "aviso", "dos rotulos seguidos (solo vale si es un eco)")
        ant_rot = bool(r) and e.get("plano") != "pensamiento"

        d = duracion_estimada(e)
        if d > MAX_SEG + 0.2:
            p(e, "aviso", f"plano de {d:.1f} s, demasiado largo")
        elif d < 1.2:
            p(e, "aviso", f"plano de {d:.1f} s, demasiado corto")
    return problemas


def verificar_imagen(escena, refs=None, log=print):
    """Le pregunta al modelo si la imagen generada cumple. Cuesta centimos.
    Si hay hojas de referencia, tambien comprueba que los personajes/objetos
    de la imagen tengan la misma forma y color que en esas hojas (si no, ese
    plano se re-hace en vez de quedarse con un diseño distinto sin avisar)."""
    from google.genai import types
    img = ruta_img(escena["id"])
    if not img.exists():
        return {"texto_ok": False, "estilo_ok": False, "consistencia_ok": False, "nota": "no existe"}
    rot = (escena.get("rotulo") or "").strip()
    intro_ref = ("Las primeras imagenes adjuntas son la hoja de referencia oficial de "
                 "los personajes/objetos recurrentes de este video. " if refs else "")
    pregunta = f"""{intro_ref}Mira la ULTIMA imagen adjunta (la generada para este plano)
y responde en JSON.

texto_ok: {'true si la imagen contiene EXACTAMENTE el texto "' + rot + '", bien escrito, sin faltas y sin otras palabras. false en cualquier otro caso.' if rot else 'true si la imagen NO contiene ninguna letra ni numero. false si contiene texto.'}
estilo_ok: true si es un dibujo plano de trazo negro grueso, sin sombreado, sin
degradados y sin textura. false si tiene sombras, volumen o aspecto realista.
consistencia_ok: {'true si cada personaje/objeto reconocible de la ultima imagen tiene la MISMA forma, proporciones y color que en las hojas de referencia. false si alguno sale con otra forma, otro color o otras proporciones claramente distintas.' if refs else '(no hay hojas de referencia con las que comparar: pon true)'}
nota: una frase muy corta en espanol diciendo que falla, o cadena vacia."""
    esquema = _S(type=_T().OBJECT, required=["texto_ok", "estilo_ok", "consistencia_ok", "nota"],
                 properties={"texto_ok": _S(type=_T().BOOLEAN),
                             "estilo_ok": _S(type=_T().BOOLEAN),
                             "consistencia_ok": _S(type=_T().BOOLEAN),
                             "nota": _S(type=_T().STRING)})
    try:
        contenido = list(refs or []) + [
            types.Part.from_bytes(data=img.read_bytes(), mime_type="image/png"),
            types.Part.from_text(text=pregunta)]
        r = cliente().models.generate_content(
            model=MODELO_TEXTO, contents=contenido,
            config=types.GenerateContentConfig(response_mime_type="application/json",
                                               response_schema=esquema))
        return json.loads(r.text)
    except Exception as ex:
        log(f"  no se pudo verificar el plano {escena['id']}: {type(ex).__name__}")
        return {"texto_ok": True, "estilo_ok": True, "consistencia_ok": True, "nota": ""}


def generar_imagen(escena, refs=None, log=print, modelo=None):
    from google.genai import types
    modelo = modelo or MODELO_IMG
    refs = _refs_de(escena) if refs is None else refs
    destino = ruta_img(escena["id"])
    destino.parent.mkdir(parents=True, exist_ok=True)
    prompt = prompt_de(escena)
    contenido = refs + [types.Part.from_text(text=prompt)]
    for intento in range(3):
        for config in _configs_img(types):     # varias formas, por si la libreria cambia
            try:
                r = cliente().models.generate_content(
                    model=modelo, contents=contenido, config=config)
                data = next((p.inline_data.data
                             for p in r.candidates[0].content.parts if p.inline_data), None)
                if not data:
                    raise RuntimeError("la respuesta no trae imagen")
                destino.write_bytes(data)
                _registrar_gasto(f"imagen {escena['id']:03d}", COSTE_IMG.get(modelo, 0))
                return destino
            except TypeError:
                continue                        # esa forma de config no existe, prueba otra
            except Exception as ex:
                log(f"fallo plano {escena['id']} (intento {intento+1}): "
                    f"{type(ex).__name__}: {ex}")
                break
        time.sleep(5)
    raise RuntimeError(f"plano {escena['id']} sin imagen tras 3 intentos")


def generar_hoja_referencia(descripcion, destino, paleta=None, log=print):
    """Genera una hoja de referencia (varias vistas, fondo blanco) de UN
    elemento recurrente (personaje, animal u objeto). Sin esto, el modelo
    reinterpreta la descripcion de texto desde cero en cada plano y el
    elemento sale distinto cada vez; con la hoja como referencia de imagen,
    se mantiene consistente en forma Y color en los 100+ planos del video."""
    from google.genai import types
    paleta_txt = (f" Use ONLY these colors where they apply: {', '.join(paleta)}."
                  if paleta else "")
    prompt = (
        "Reference sheet, turnaround style, for a recurring element in a "
        "hand-drawn doodle explainer video (it can be a character, an animal "
        "or an object). Plain white background. Draw the SAME element 3-4 "
        "times, from different simple angles or in a close-up, so its exact "
        "shape, proportions, colors and details are unambiguous and "
        "repeatable across many future drawings that will copy this design "
        "exactly.\n\n"
        f"Element: {descripcion}\n\n"
        f"Style: {ESTILO}{paleta_txt}\n"
        "No text, no labels, no other props of any kind, just the element "
        "repeated on white."
    )
    contenido = [types.Part.from_text(text=prompt)]
    destino.parent.mkdir(parents=True, exist_ok=True)
    for intento in range(3):
        for config in _configs_img(types):
            try:
                r = cliente().models.generate_content(
                    model=MODELO_IMG, contents=contenido, config=config)
                data = next((p.inline_data.data
                             for p in r.candidates[0].content.parts if p.inline_data), None)
                if not data:
                    raise RuntimeError("la respuesta no trae imagen")
                destino.write_bytes(data)
                _registrar_gasto(f"hoja de referencia {destino.stem}", COSTE_IMG.get(MODELO_IMG, 0))
                log(f"hoja de referencia lista: {destino}")
                return destino
            except TypeError:
                continue
            except Exception as ex:
                log(f"fallo hoja de referencia (intento {intento+1}): {type(ex).__name__}: {ex}")
                break
        time.sleep(5)
    raise RuntimeError("hoja de referencia sin imagen tras 3 intentos")


def prompt_referencia_humano(descripcion, paleta=None):
    """Prompt completo, listo para copiar y pegar en Google Flow/ChatGPT/
    Midjourney/lo que sea, para que la persona genere ELLA MISMA la imagen de
    referencia de un elemento. Junta el mismo estilo/paleta que usa el resto
    del video (para que la referencia encaje) con instrucciones explicitas de
    encuadre, pose y fondo -- cuanto mas especifico este prompt, menos
    probable que la imagen subida no sirva y haya que repetir el paso."""
    paleta_txt = (f" Background: one flat color from this palette, whichever "
                  f"fits best: {', '.join(paleta)}." if paleta else
                  " Background: one single flat color, no gradient.")
    return (
        f"Style: {ESTILO}{paleta_txt}\n\n"
        f"Subject: {descripcion}\n\n"
        "Framing: a single, clear, front-facing (or clean 3/4) full view of "
        "the subject alone, centered, neutral standing/sitting/resting pose "
        "-- nothing dynamic or mid-action. Nothing else in the frame: no "
        "other characters, no props, no text, no labels. This image's only "
        "job is to nail down exactly how this element looks, so every other "
        "shot in the video can copy it precisely."
    )


def _configs_img(types):
    """Formas de pedir una imagen 16:9, de la mas completa a la mas basica."""
    formas = []
    try:
        formas.append(types.GenerateContentConfig(
            response_modalities=["IMAGE"],
            image_config=types.ImageConfig(aspect_ratio="16:9")))
    except Exception:
        pass
    try:
        formas.append(types.GenerateContentConfig(response_modalities=["TEXT", "IMAGE"]))
    except Exception:
        pass
    formas.append(types.GenerateContentConfig())
    return formas


def verificar_solo(escenas=None, ids=None, log=print):
    """Comprueba imagenes YA generadas sin volver a generarlas (barato: solo
    la llamada de vision, no la de imagen). Util para auditar de golpe un
    lote que se genero con verificar=False y saber cuales hay que rehacer."""
    escenas = escenas or cargar_escenas()
    refs = _refs()
    objetivo = set(ids) if ids else None
    malas = []
    for e in escenas:
        if parado():
            log("detenido por el usuario")
            break
        if objetivo is not None and e["id"] not in objetivo:
            continue
        if not ruta_img(e["id"]).exists():
            continue
        v = verificar_imagen(e, refs, log)
        e["check"] = v
        if not (v.get("texto_ok") and v.get("estilo_ok") and v.get("consistencia_ok")):
            malas.append(e["id"])
            log(f"  plano {e['id']:03d}: {v.get('nota','')}")
    guardar_escenas(escenas)
    log(f"{len(malas)} planos para revisar/rehacer: {malas}")
    return malas


def generar_imagenes(escenas=None, solo=None, ids=None, rehacer=False,
                     verificar=True, log=print):
    """Genera cada imagen UNA sola vez (nunca reintenta sola por dentro: eso
    era lo que hacia que la app "de un tiron" regenerase cosas sin
    preguntar). Si verificar=True, solo COMPRUEBA y marca cual plano no paso
    el control -- la decision de regenerar ese plano concreto es tuya,
    despues, a mano (mismo mecanismo que 'ids': le pasas la lista exacta que
    tu elijas y esa llamada SI usa el modelo mas capaz, porque asumimos que
    es una correccion consciente, no un reintento automatico)."""
    escenas = escenas or cargar_escenas()
    biblia = cargar_biblia()
    if not archivos_ref():
        log("AVISO: carpeta 'referencias/' vacia -> el estilo variara entre planos.")
    objetivo = set(ids or ([solo] if solo else []))
    modelo = MODELO_IMG_RETRY if ids else MODELO_IMG
    hechas, malas = 0, []
    for e in escenas:
        if parado():
            log("detenido por el usuario")
            break
        if objetivo and e["id"] not in objetivo:
            continue
        if not objetivo and ruta_img(e["id"]).exists() and not rehacer:
            continue
        refs = _refs_de(e, biblia)
        try:
            generar_imagen(e, refs, log, modelo=modelo)
            if verificar:
                v = verificar_imagen(e, refs, log)
                e["check"] = v
                if not (v.get("texto_ok") and v.get("estilo_ok") and v.get("consistencia_ok")):
                    fallo = ("texto" if not v.get("texto_ok") else "") + \
                            ("/estilo" if not v.get("estilo_ok") else "") + \
                            ("/consistencia" if not v.get("consistencia_ok") else "")
                    log(f"  plano {e['id']:03d} no pasa el control ({fallo.strip('/')}"
                        f"{': ' + v['nota'] if v.get('nota') else ''}) "
                        f"-- queda marcado, no se regenera solo")
                    malas.append(e["id"])
            else:
                e.pop("check", None)
            hechas += 1
            log(f"imagen {e['id']:03d} lista")
        except Exception as ex:
            log(f"ERROR plano {e['id']}: {ex}")
            malas.append(e["id"])
    _guardar_checks(escenas)
    log(f"{hechas} imagenes generadas")
    if malas:
        log(f"REVISAR estos planos cuando quieras (tu decides si regenerarlos): {malas}")


# --------------------------------------------------------------------- voz
def _frases_unicas(escenas):
    """[(frase_id, texto_frase)] en orden, sin repetir."""
    vistas, salida = set(), []
    for e in escenas:
        if e["frase_id"] not in vistas:
            vistas.add(e["frase_id"])
            salida.append((e["frase_id"], e.get("texto_frase", e["texto"])))
    return salida


def ruta_alineacion(frase_id):
    return AUDIO / f"f{frase_id:03d}.json"


def generar_voz_frase(frase_id, texto, motor="eleven", voz=None, rate="+0%"):
    mp3 = ruta_audio(frase_id)
    mp3.parent.mkdir(parents=True, exist_ok=True)
    if motor == "eleven":
        voz = voz or voz_configurada() or os.environ.get("ELEVENLABS_VOICE_ID", "")
        import requests, base64
        r = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voz}/with-timestamps",
            headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"]},
            json={"text": texto, "model_id": "eleven_multilingual_v2",
                  "voice_settings": {"stability": 0.45, "similarity_boost": 0.75,
                                     "style": 0.35, "use_speaker_boost": True}},
            timeout=120)
        r.raise_for_status()
        d = r.json()
        mp3.write_bytes(base64.b64decode(d["audio_base64"]))
        alineacion = d.get("normalized_alignment") or d.get("alignment")
        if alineacion:
            ruta_alineacion(frase_id).write_text(
                json.dumps(alineacion, ensure_ascii=False), encoding="utf-8")
        _registrar_gasto(f"voz frase {frase_id:03d}", len(texto) / 1000 * COSTE_VOZ_1000)
    else:
        subprocess.run(
            ["edge-tts", "--voice", voz or "es-MX-JorgeNeural", "--rate", rate,
             "--text", texto, "--write-media", str(mp3)], check=True)
        ruta_alineacion(frase_id).unlink(missing_ok=True)  # edge-tts no da timestamps
    return mp3


def generar_voces(escenas=None, motor="eleven", voz=None, solo=None,
                  rehacer=False, log=print):
    escenas = escenas or cargar_escenas()
    if motor == "eleven":
        log(f"voz ElevenLabs: {voz or voz_configurada() or os.environ.get('ELEVENLABS_VOICE_ID', '(vacia)')}")
    objetivo = None
    if solo:
        e = next((x for x in escenas if x["id"] == solo), None)
        objetivo = e["frase_id"] if e else None
    hechas = 0
    for fid, texto in _frases_unicas(escenas):
        if parado():
            log("detenido por el usuario")
            break
        if objetivo and fid != objetivo:
            continue
        if ruta_audio(fid).exists() and not rehacer and not objetivo:
            continue
        try:
            generar_voz_frase(fid, texto, motor, voz)
            hechas += 1
            log(f"audio f{fid:03d} listo")
        except Exception as ex:
            log(f"ERROR frase {fid}: {ex}")
    log(f"{hechas} audios generados")


# ------------------------------------------------------------------ montar
def _dur(mp3):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(mp3)],
        capture_output=True, text=True, check=True).stdout
    return float(out.strip())


# --------------------------------------------------------------- efectos
# Efectos sintetizados con ffmpeg (nada de descargas: cero dudas de derechos
# de autor y cero coste). Deliberadamente abstractos/percusivos, no imitan
# sonidos organicos (un "meow" o un ronroneo sintetizado con senos suena
# falso): son subrayados de edicion, como en un documental. Se enganchan a
# señales que ya existen en el storyboard, no a una lectura semantica libre:
#   - "sting"   -> el tramo es "revelacion" (la prueba, el mecanismo)
#   - "impacto" -> el tramo es "factura" (las consecuencias, golpe seco)
#   - "chime"   -> el tramo es "tesis" o "cta" (conclusion, campanita clara)
#   - "tick"    -> el plano es de tipo "recuento" (cuenta/repeticion)
#   - "pop"     -> aparece un rotulo en el plano (y no coincide con otra señal)
EFECTOS_DIR = pathlib.Path("efectos")
EFECTOS = {"pop": EFECTOS_DIR / "pop.mp3",
           "sting": EFECTOS_DIR / "sting.mp3",
           "impacto": EFECTOS_DIR / "impacto.mp3",
           "chime": EFECTOS_DIR / "chime.mp3",
           "tick": EFECTOS_DIR / "tick.mp3"}
_EFECTOS_DEF = {
    "pop": "aevalsrc=exprs='sin(2*PI*1500*t)*exp(-40*t)+0.5*sin(2*PI*220*t)*exp(-25*t)':d=0.14:s=44100",
    "sting": "aevalsrc=exprs='0.6*sin(2*PI*(320+900*t)*t)*exp(-2.2*t)':d=0.55:s=44100",
    "impacto": "aevalsrc=exprs='0.9*sin(2*PI*85*t)*exp(-7*t)+0.3*sin(2*PI*140*t)*exp(-12*t)':d=0.45:s=44100",
    "chime": "aevalsrc=exprs='0.5*sin(2*PI*880*t)*exp(-3*t)+0.3*sin(2*PI*1320*t)*exp(-3.5*t)':d=0.6:s=44100",
    "tick": "aevalsrc=exprs='0.7*sin(2*PI*2500*t)*exp(-90*t)':d=0.08:s=44100",
}


def generar_efectos(log=print):
    """Crea los mp3 de efectos si todavia no existen. Idempotente y gratis:
    solo sintesis local con ffmpeg, no llama a ninguna API."""
    EFECTOS_DIR.mkdir(exist_ok=True)
    for nombre, fuente in _EFECTOS_DEF.items():
        destino = EFECTOS[nombre]
        if destino.exists():
            continue
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", fuente,
             "-af", "volume=0.9", str(destino)], check=True)
        log(f"efecto {nombre} generado -> {destino}")


def _efecto_de(escena, transiciones):
    """El efecto (si toca alguno) para este plano concreto. El giro narrativo
    manda sobre el simple rotulo: si coinciden en el mismo plano, es mas
    importante marcar la revelacion/factura que el chasquido del texto."""
    def _si_existe(nombre):
        p = EFECTOS.get(nombre)
        return p if p and p.exists() else None
    fn = transiciones.get(escena["id"])
    if fn == "revelacion":
        return _si_existe("sting")
    if fn == "factura":
        return _si_existe("impacto")
    if fn in ("tesis", "cta"):
        return _si_existe("chime")
    if escena.get("plano") == "recuento":
        return _si_existe("tick")
    if (escena.get("rotulo") or "").strip():
        return _si_existe("pop")
    return None


def _tiempos_reales(frase_id, compases):
    """[(t_ini, t_fin), ...] en segundos REALES para cada compas de la frase,
    leidos de los timestamps por caracter que devuelve ElevenLabs (no una
    estimacion por numero de palabras). None si no hay alineacion guardada
    (edge-tts, o audio generado antes de este cambio) o si el texto no casa
    con fiabilidad (ElevenLabs normalizo numeros/abreviaturas): en ese caso
    es mas seguro caer al reparto por palabras que fiarse de un casamiento
    dudoso."""
    p = ruta_alineacion(frase_id)
    if not p.exists():
        return None
    try:
        al = json.loads(p.read_text(encoding="utf-8"))
        texto_al = "".join(al["characters"])
        ini_t = al["character_start_times_seconds"]
        fin_t = al["character_end_times_seconds"]
    except Exception:
        return None
    cursor, tiempos = 0, []
    for c in compases:
        pos = texto_al.find(c.strip(), cursor)
        if pos < 0:
            return None
        fin = pos + len(c.strip())
        tiempos.append((ini_t[pos], fin_t[fin - 1]))
        cursor = fin
    return tiempos


# cuanto se retrasa cada cambio de imagen respecto al timestamp exacto de la
# voz (ver el comentario en montar_video): un compas cortado justo al
# milisegundo real se siente demasiado brusco cuando hay varios compases
# cortos seguidos.
MARGEN_TRANSICION = 0.2


def montar_video(escenas=None, forzar=False, log=print):
    """Cada frase tiene un mp3; sus compases se reparten su duracion REAL
    segun los timestamps de ElevenLabs (ver _tiempos_reales), o por numero de
    palabras si no hay alineacion disponible para esa frase.
    Sin efectos de sonido: se probaron y no convencieron, solo queda la voz.

    IMPORTANTE: el audio de cada frase NUNCA se trocea ni se vuelve a
    codificar. Antes cada clip de imagen llevaba pegado su propio trocito de
    mp3 (cortado con -ss y recodificado a AAC), y eso causaba dos fallos
    reales en produccion: (a) cada recodificacion añade un micro-silencio de
    "priming" del codec AAC al principio del trozo, oyendose como un corte
    justo al empezar cada fragmento; (b) el trocito de audio (cortado de un
    mp3 con -ss, que no es sample-accurate) a veces salia mas corto de lo
    esperado y el "-shortest" recortaba el CLIP DE VIDEO entero a esa
    duracion, encadenando varios planos demasiado cortos seguidos (el efecto
    de "las imagenes pasan rapidisimo y no cuadran con la voz"). Ahora se
    generan los clips de video MUDOS (duracion exacta, sin audio), se
    concatenan aparte, el audio de cada frase se concatena tal cual sin
    recodificar, y solo al final se juntan video mudo + audio completo en un
    unico paso."""
    escenas = sorted(escenas or cargar_escenas(), key=lambda e: e["id"])
    CLIPS.mkdir(parents=True, exist_ok=True)

    lineas_video = []
    lineas_audio = []

    # no montar a medias sin avisar
    sin_img = [e["id"] for e in escenas if not ruta_img(e["id"]).exists()]
    sin_aud = sorted({e["frase_id"] for e in escenas if not ruta_audio(e["frase_id"]).exists()})
    if (sin_img or sin_aud) and not forzar:
        raise RuntimeError(
            f"faltan {len(sin_img)} imagenes {sin_img[:12]}{'...' if len(sin_img)>12 else ''} "
            f"y {len(sin_aud)} audios {sin_aud[:12]}{'...' if len(sin_aud)>12 else ''}. "
            "Genera lo que falta, o monta de todas formas si es a proposito.")

    porf = {}
    for e in escenas:
        porf.setdefault(e["frase_id"], []).append(e)

    for fid in sorted(porf):
        grupo = porf[fid]
        mp3 = ruta_audio(fid)
        if not mp3.exists():
            log(f"salto frase {fid}: falta audio")
            continue
        total = _dur(mp3)
        reales = _tiempos_reales(fid, [e["texto"] for e in grupo])
        if reales:
            log(f"  frase {fid}: sincronizada con timestamps reales de la voz")
        elif len(grupo) > 1:
            # con un solo compas en la frase el reparto proporcional da
            # igual (se lleva el audio entero); con varios, esto es la unica
            # frase del video que NO tiene sincronizacion real, avisa
            log(f"  AVISO frase {fid}: sin timestamps reales (audio antiguo o "
                f"no caso el texto), reparto estimado por palabras para sus "
                f"{len(grupo)} planos")
        pesos = [max(1, len(e["texto"].split())) for e in grupo]
        suma = sum(pesos)
        t = 0.0
        huecos = False
        cortes = None
        if reales:
            # puntos de corte entre compases: por defecto se retrasa
            # MARGEN_TRANSICION respecto al milisegundo exacto en que empieza
            # la siguiente palabra (cortando justo en el timestamp real, la
            # imagen anterior desaparece en el mismo instante en que se
            # empieza a decir la frase siguiente, sin darle al espectador ni
            # un instante para verla -- se nota sobre todo en tramos con
            # compases cortos seguidos). PERO si el compas que va a ENTRAR
            # lleva un rotulo/bocadillo, no se retrasa nada: un rotulo es un
            # golpe de efecto pensado para caer justo en la palabra (ej.
            # "CHAIN REACTION" apareciendo el instante en que se dice, no
            # 0.2s despues) y el margen ahi se notaba como que la imagen iba
            # "atrasada" respecto a la voz. El primer y ultimo compas de la
            # frase son anclas fijas (no pueden moverse sin descuadrar el
            # audio real de la frase).
            cortes = [reales[0][0]]
            for j in range(1, len(grupo)):
                tiene_rotulo = bool((grupo[j].get("rotulo") or "").strip()
                                     or (grupo[j].get("rotulo2") or "").strip())
                cortes.append(reales[j][0] + (0.0 if tiene_rotulo else MARGEN_TRANSICION))
            cortes.append(total)
        for k, e in enumerate(grupo):
            img = ruta_img(e["id"])
            if not img.exists():
                log(f"salto plano {e['id']}: falta imagen")
                t += total * pesos[k] / suma
                huecos = True
                continue
            if reales:
                t = cortes[k]
                d = cortes[k + 1] - t
            else:
                d = total - t if k == len(grupo) - 1 else total * pesos[k] / suma
            d = max(0.4, round(d, 3))
            clip = CLIPS / f"{e['id']:03d}.mp4"
            frames = max(1, round(d * 30))
            vf = (f"scale=4000:-1,zoompan=z='min(zoom+0.0006,1.15)':d={frames}:"
                  f"s=1920x1080:fps=30,setsar=1")
            # el -t va DESPUES del filtro (lado de salida), no antes de -i: con
            # "-loop 1 -t d -i img" el demuxer de imagen sigue alimentando
            # fotogramas de entrada durante d segundos, y zoompan multiplica
            # su "d=frames" por CADA fotograma de entrada que recibe -- con
            # audio pegado y -shortest esto quedaba oculto (se recortaba solo
            # al retirar el audio), pero sin el hizo que cada clip durara
            # segundos/minutos de mas (comprobado: un d=0.987s generaba un
            # clip real de 25s). Cortando en la salida, zoompan solo procesa
            # el primer fotograma antes de que se alcance el limite.
            cmd = ["ffmpeg", "-y", "-loglevel", "error",
                   "-loop", "1", "-i", str(img),
                   "-vf", vf, "-t", f"{d:.3f}",
                   "-c:v", "libx264", "-pix_fmt", "yuv420p",
                   "-an", str(clip)]
            subprocess.run(cmd, check=True)
            lineas_video.append(f"file '{clip.resolve()}'")
            log(f"clip {e['id']:03d} listo")
            t += d
        if not huecos:
            lineas_audio.append(f"file '{mp3.resolve()}'")
        else:
            # si a esta frase le falto alguna imagen, el video mudo de este
            # tramo no dura lo mismo que el mp3 entero: mejor no pegar el
            # audio completo (se desincronizaria) que dejarlo tal cual.
            log(f"  AVISO frase {fid}: faltan imagenes en el grupo, el audio "
                f"de esta frase no se incluye en el montaje")

    if not lineas_video:
        raise RuntimeError("no hay ningun clip: faltan imagenes o audios")

    lista_v = OUT / "concat_video.txt"
    lista_v.write_text("\n".join(lineas_video), encoding="utf-8")
    video_mudo = OUT / "video_mudo.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
         "-i", str(lista_v), "-c", "copy", str(video_mudo)], check=True)

    lista_a = OUT / "concat_audio.txt"
    lista_a.write_text("\n".join(lineas_audio), encoding="utf-8")
    audio_completo = OUT / "audio_completo.mp3"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
         "-i", str(lista_a), "-c", "copy", str(audio_completo)], check=True)

    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-i", str(video_mudo), "-i", str(audio_completo),
         "-c:v", "copy", "-c:a", "aac", "-shortest", str(VIDEO)], check=True)

    # control final: duracion real contra la suma de los audios
    real = _dur(VIDEO)
    audios = sum(_dur(ruta_audio(f)) for f in porf if ruta_audio(f).exists())
    log(f"video listo: {VIDEO}")
    log(f"duracion {int(real//60)}:{int(real%60):02d} · {len(lineas_video)} clips · "
        f"desfase con el audio {real-audios:+.2f} s")
    if abs(real - audios) > 1.5:
        log("AVISO: el video y el audio no cuadran, revisa si falto algun plano")
    return VIDEO


# ----------------------------------------------------------------------- seo
CAPITULO_MIN_S = 10   # youtube exige minimo 10s entre capitulos y el primero en 0:00


def _duraciones_reales_o_estimadas(escenas):
    """Duracion de cada plano: real (si ya hay audio) o estimada por palabras."""
    porf = {}
    for e in escenas:
        porf.setdefault(e["frase_id"], []).append(e)
    dur = {}
    for fid, grupo in porf.items():
        mp3 = ruta_audio(fid)
        if mp3.exists():
            total = _dur(mp3)
            pesos = [max(1, len(e["texto"].split())) for e in grupo]
            suma = sum(pesos)
            for e, w in zip(grupo, pesos):
                dur[e["id"]] = total * w / suma
        else:
            for e in grupo:
                dur[e["id"]] = duracion_estimada(e)
    return dur


def _capitulos_brutos(escenas):
    """Tramos por cambio de funcion narrativa, con su segundo de inicio."""
    escenas = sorted(escenas, key=lambda e: e["id"])
    dur = _duraciones_reales_o_estimadas(escenas)
    t, ant, tramos = 0.0, None, []
    for e in escenas:
        fn = e.get("funcion", "contexto")
        if fn != ant:
            tramos.append({"t": t, "id": e["id"], "funcion": fn,
                           "texto": e.get("texto_frase", e["texto"])})
            ant = fn
        t += dur[e["id"]]
    tramos = [c for i, c in enumerate(tramos)
              if i == 0 or c["t"] - tramos[i - 1]["t"] >= CAPITULO_MIN_S]
    return tramos, t


def cargar_seo():
    return json.loads(SEO.read_text(encoding="utf-8")) if SEO.exists() else None


def generar_seo(escenas=None, texto_guion=None, log=print):
    """Titulos, descripcion con gancho, capitulos y tags. Una llamada de texto, centimos."""
    escenas = escenas or cargar_escenas()
    if texto_guion is None:
        texto_guion = GUION.read_text(encoding="utf-8") if GUION.exists() else ""
    tramos, total = _capitulos_brutos(escenas)
    aprox = not all(ruta_audio(fid).exists() for fid, _ in _frases_unicas(escenas))

    T = _T()
    esquema = _S(type=T.OBJECT,
                 required=["titulos", "descripcion_gancho", "tags", "capitulos"],
                 properties={
                     "titulos": _S(type=T.ARRAY, items=_S(type=T.STRING)),
                     "descripcion_gancho": _S(type=T.STRING),
                     "tags": _S(type=T.ARRAY, items=_S(type=T.STRING)),
                     "capitulos": _S(type=T.ARRAY, items=_S(
                         type=T.OBJECT, required=["id", "titulo"],
                         properties={"id": _S(type=T.INTEGER),
                                    "titulo": _S(type=T.STRING)})),
                 })
    lineas = "\n".join(f'{c["id"]}. [{c["funcion"]}] {c["texto"]}' for c in tramos)
    idioma_es = _es_espanol(texto_guion)
    aviso_idioma = (
        "IMPORTANTE: el guion esta en ESPAÑOL. Escribe titulos, descripcion, "
        "tags y capitulos en ESPAÑOL." if idioma_es else
        "IMPORTANT: the script below is in ENGLISH. Write the titles, "
        "description, tags and chapters in ENGLISH, not Spanish -- the "
        "audience for this video speaks English.")
    instruccion = f"""{aviso_idioma}

Eres un experto en SEO de YouTube. Con este guion de un video de
divulgacion (estilo documental corto, dibujado con monigotes), genera TODO
(titulos, descripcion, tags, capitulos) en el MISMO IDIOMA en que esta
escrito el guion de abajo -- no lo traduzcas, el publico objetivo es el de
ese idioma:

- 3 titulos alternativos: gancho + cifra o dato concreto, menos de 70 caracteres,
  sin clickbait que el video no cumpla luego.
- Un parrafo de 3-4 frases para las dos primeras lineas de la descripcion de
  YouTube (lo unico que se ve sin pulsar "mas"): el gancho, con las palabras
  clave de busqueda metidas de forma natural, no como lista.
- Entre 15 y 20 tags de busqueda en el idioma del guion (temas, nombres propios,
  conceptos clave), sin almohadillas y sin duplicar el contenido de los titulos.
- Un titulo de capitulo de YouTube (3 a 6 palabras) para cada uno de estos
  tramos del guion, fiel a lo que se cuenta ahi, uno por cada id:
{lineas}

GUION COMPLETO:
{texto_guion[:6000]}"""
    r = _json(instruccion, esquema, log, "seo")
    if not r:
        log("AVISO: no se pudo generar el SEO, intentalo de nuevo.")
        return None

    titulos_cap = {int(c["id"]): c["titulo"] for c in r.get("capitulos", [])}
    capitulos = []
    for c in tramos:
        m, s = divmod(int(c["t"]), 60)
        capitulos.append(f'{m}:{s:02d} {titulos_cap.get(c["id"], c["funcion"].capitalize())}')

    salida = {
        "titulos": r.get("titulos", []),
        "descripcion_gancho": r.get("descripcion_gancho", ""),
        "capitulos": capitulos,
        "capitulos_aproximados": aprox,
        "tags": r.get("tags", []),
        "duracion_total": round(total),
    }
    OUT.mkdir(exist_ok=True)
    SEO.write_text(json.dumps(salida, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"SEO listo: {len(salida['titulos'])} titulos, {len(capitulos)} capitulos"
        + (" (duracion aproximada, faltan audios)" if aprox else "")
        + f", {len(salida['tags'])} tags -> {SEO}")
    return salida


# ------------------------------------------------------------ puntuacion SEO
def puntuar_seo(seo=None, log=print):
    """Puntua titulo/descripcion/tags al estilo de un plugin tipo vidIQ: una
    nota de 0 a 100 y una sugerencia CONCRETA (no generica) para cada uno.
    Una sola llamada de texto, centimos. Si algo ya esta bien, lo dice --
    no inventa cambios porque si."""
    seo = seo or cargar_seo()
    if not seo:
        log("AVISO: no hay SEO generado todavia (titulo/descripcion/tags).")
        return None
    T, S = _T(), _S
    campo = lambda: S(type=T.OBJECT, required=["nota", "sugerencia"],
                       properties={"nota": S(type=T.INTEGER),
                                   "sugerencia": S(type=T.STRING)})
    esquema = S(type=T.OBJECT, required=["titulo", "descripcion", "tags"],
                properties={"titulo": campo(), "descripcion": campo(), "tags": campo()})
    titulo_actual = (seo.get("titulos") or [""])[0]
    idioma_es = _es_espanol(titulo_actual + " " + seo.get("descripcion_gancho", ""))
    aviso_idioma = ("Responde en ESPAÑOL." if idioma_es else
                     "The title/description below are in ENGLISH -- write your "
                     "nota/sugerencia feedback in ENGLISH too, matching that audience.")
    instruccion = f"""Actua como un experto en SEO/CTR de YouTube (el mismo tipo de
analisis que hace un plugin como vidIQ o TubeBuddy). {aviso_idioma}

Puntua cada elemento del 0 al 100 segun estos criterios EXACTOS, y da una
sugerencia CONCRETA Y LISTA PARA USAR (el texto completo que deberia poner,
no un consejo abstracto tipo "mejora el titulo"). Si algo ya cumple bien el
criterio, dilo y pon nota alta -- no inventes cambios porque si.

- titulo: longitud ideal 40-70 caracteres (se corta en buscador/sugeridos si
  se pasa mucho), incluye un dato o gancho concreto (numero, pregunta
  fuerte, cifra), la palabra clave principal cerca del principio, sin
  mayusculas ni signos de exclamacion de spam.
- descripcion: las 2 primeras frases (lo unico visible antes de "mostrar
  mas") deben llevar el gancho + la palabra clave principal de forma
  natural, longitud total generosa (no una linea suelta).
- tags: cuenta y variedad (10-20 ideal), presencia de variantes de la
  palabra clave principal y de terminos de busqueda relacionados reales
  (no inventados ni genericos de relleno).

TITULO ACTUAL: {titulo_actual}
DESCRIPCION ACTUAL: {seo.get('descripcion_gancho', '')}
TAGS ACTUALES ({len(seo.get('tags', []))}): {', '.join(seo.get('tags', []))}"""
    r = _json(instruccion, esquema, log, "puntuar_seo")
    if not r:
        log("AVISO: no se pudo puntuar el SEO, intentalo de nuevo.")
        return None
    OUT.mkdir(exist_ok=True)
    (OUT / "puntuacion_seo.json").write_text(
        json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"Puntuacion SEO: titulo {r['titulo']['nota']}, "
        f"descripcion {r['descripcion']['nota']}, tags {r['tags']['nota']}")
    return r


def generar_miniatura(texto_guion=None, n=2, log=print):
    """Genera N miniaturas candidatas (1280x720) a partir del guion, usando
    las mismas referencias/paleta que el resto del video para que encajen
    visualmente, pero con reglas de composicion pensadas para el click (no
    para narrar una escena): un solo sujeto claro y expresivo llenando la
    mayor parte del encuadre, alto contraste, muy poco o ningun texto."""
    from google.genai import types
    if texto_guion is None:
        texto_guion = GUION.read_text(encoding="utf-8") if GUION.exists() else ""
    biblia = cargar_biblia()
    paleta = biblia.get("paleta") or []
    paleta_txt = (f" Palette: ONLY these colors, nothing else: {', '.join(paleta)}."
                  if paleta else "")
    elementos_txt = "; ".join(f"{e['nombre']} ({e['descripcion']})"
                              for e in biblia.get("elementos", []))
    refs = archivos_ref()
    destinos = []
    for i in range(n):
        variante = ("Variante mas expresiva/exagerada en la pose y la mirada."
                    if i % 2 else "Variante mas limpia y directa, sujeto centrado.")
        prompt = (
            "YouTube thumbnail (1280x720), NOT a regular narrative frame from "
            "the video. Composition rules for maximum click-through: ONE clear "
            "subject (the protagonist or the single most striking element of "
            "the story), large, filling most of the frame, strong contrast "
            "against the background, no clutter, no secondary props competing "
            "for attention. The subject's expression should convey surprise, "
            "curiosity or intensity -- the kind of look that makes someone "
            "stop scrolling. At most 3-4 bold words of text if it genuinely "
            "adds punch, otherwise no text at all. " + variante + "\n\n"
            f"Video topic (do not illustrate a random scene from this, use it "
            f"only to pick the subject and mood): {texto_guion[:600]}\n\n"
            f"Recurring elements available (use one if it fits, copy its exact "
            f"design from the reference images): {elementos_txt}\n\n"
            f"Estilo: {ESTILO}{paleta_txt}"
        )
        if refs:
            prompt += ("\n\nUsa las imagenes adjuntas como referencia OBLIGATORIA "
                       "para cualquier personaje/objeto reconocible: copia su "
                       "diseño exacto, no lo reinterpretes.")
        contenido = refs + [types.Part.from_text(text=prompt)]
        destino = OUT / f"miniatura_{i+1}.jpeg"
        ok = False
        for intento in range(3):
            for config in _configs_img(types):
                try:
                    r = cliente().models.generate_content(
                        model=MODELO_IMG_RETRY, contents=contenido, config=config)
                    data = next((p.inline_data.data
                                 for p in r.candidates[0].content.parts if p.inline_data), None)
                    if not data:
                        raise RuntimeError("la respuesta no trae imagen")
                    destino.parent.mkdir(parents=True, exist_ok=True)
                    destino.write_bytes(data)
                    _registrar_gasto(f"miniatura {i+1}", COSTE_IMG.get(MODELO_IMG_RETRY, 0))
                    log(f"miniatura {i+1}/{n} lista: {destino}")
                    destinos.append(destino)
                    ok = True
                    break
                except TypeError:
                    continue
                except Exception as ex:
                    log(f"fallo miniatura {i+1} (intento {intento+1}): {type(ex).__name__}: {ex}")
                    break
            if ok:
                break
            time.sleep(5)
    return destinos


def puntuar_miniatura(ruta, log=print):
    """Puntua una miniatura ya generada con vision, del 0 al 100, igual que
    lo haria un experto en CTR. Cuesta centimos (una llamada de vision)."""
    from google.genai import types
    if not ruta.exists():
        return {"nota": 0, "sugerencia": "no existe la imagen"}
    T, S = _T(), _S
    esquema = S(type=T.OBJECT, required=["nota", "sugerencia"],
                properties={"nota": S(type=T.INTEGER), "sugerencia": S(type=T.STRING)})
    instruccion = """Puntua esta miniatura de YouTube del 0 al 100 como lo haria
un experto en CTR (el mismo tipo de analisis que un plugin como vidIQ).
Evalua: si hay UN sujeto claro y expresivo que destaque de inmediato, el
contraste contra un feed tipico de YouTube (miniaturas vecinas), si el texto
(si lleva) se leeria bien en tamaño muy pequeño, y si transmite curiosidad
o intensidad sin parecer spam. Da una sugerencia concreta y aplicable si la
nota no es alta; si ya esta muy bien, dilo."""
    try:
        imagen = types.Part.from_bytes(data=ruta.read_bytes(), mime_type="image/jpeg")
        r = cliente().models.generate_content(
            model=MODELO_TEXTO, contents=[imagen, types.Part.from_text(text=instruccion)],
            config=types.GenerateContentConfig(response_mime_type="application/json",
                                               response_schema=esquema))
        return json.loads(r.text)
    except Exception as ex:
        log(f"  no se pudo puntuar la miniatura: {type(ex).__name__}")
        return {"nota": 0, "sugerencia": ""}


def _fmt_srt(t):
    h, resto = divmod(t, 3600)
    m, s = divmod(resto, 60)
    ms = int(round((s - int(s)) * 1000))
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{ms:03d}"


def generar_subtitulos(escenas=None, log=print):
    """.srt en espanol, por frase, a partir de los audios ya generados. Gratis, sin IA."""
    escenas = sorted(escenas or cargar_escenas(), key=lambda e: e["id"])
    t, bloques, n = 0.0, [], 0
    for fid, texto in _frases_unicas(escenas):
        mp3 = ruta_audio(fid)
        if not mp3.exists():
            log(f"salto frase {fid}: falta audio")
            continue
        d = _dur(mp3)
        n += 1
        bloques.append(f"{n}\n{_fmt_srt(t)} --> {_fmt_srt(t + d)}\n{texto}\n")
        t += d
    if not bloques:
        raise RuntimeError("no hay audios generados: no se pueden hacer subtitulos")
    OUT.mkdir(exist_ok=True)
    SUBTITULOS.write_text("\n".join(bloques), encoding="utf-8")
    log(f"subtitulos.srt listo: {n} frases, {int(t)}s -> {SUBTITULOS}")
    return SUBTITULOS


# --------------------------------------------------------------------- cli
def _main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(required=True)

    def _escenas(ar):
        versionar()
        generar_escenas(pathlib.Path(ar.guion).read_text(encoding="utf-8"))

    a = sub.add_parser("escenas"); a.add_argument("guion")
    a.set_defaults(f=_escenas)

    def _nuevo(ar):
        texto = pathlib.Path(ar.guion).read_text(encoding="utf-8")
        nombre = crear_proyecto(texto)
        print(f"proyecto: {nombre}")
        generar_escenas(texto)

    n = sub.add_parser("nuevo"); n.add_argument("guion")
    n.set_defaults(f=_nuevo)

    b = sub.add_parser("imagenes"); b.add_argument("--solo", type=int)
    b.set_defaults(f=lambda ar: generar_imagenes(solo=ar.solo))

    c = sub.add_parser("voz")
    c.add_argument("--motor", choices=["eleven", "edge"], default="eleven")
    c.add_argument("--voz", default=None)
    c.add_argument("--solo", type=int)
    c.set_defaults(f=lambda ar: generar_voces(motor=ar.motor, voz=ar.voz, solo=ar.solo))

    d = sub.add_parser("montar"); d.set_defaults(f=lambda ar: montar_video())

    e = sub.add_parser("seo"); e.set_defaults(f=lambda ar: generar_seo())

    g = sub.add_parser("subtitulos"); g.set_defaults(f=lambda ar: generar_subtitulos())

    ar = p.parse_args()
    ar.f(ar)


if __name__ == "__main__":
    _main()