#!/usr/bin/env python3
"""
Interfaz web del pipeline. Se ejecuta en tu maquina:

    pip install fastapi uvicorn google-genai requests edge-tts
    py app.py            # abre http://127.0.0.1:8000

Usa las mismas funciones que pipeline.py: la linea de comandos y la web
trabajan sobre el mismo proyecto activo, dentro de proyectos/<slug>/.
Cada video vive en su propia carpeta (ver pipeline.crear_proyecto): al pedir
un video nuevo nunca se mezcla con las imagenes, el audio o el storyboard de
uno anterior, aunque todos queden guardados en el disco para volver a ellos.
"""
import json, os, pathlib, threading, time, traceback

# fuerza el directorio de trabajo a la carpeta de esta app ANTES de importar
# pipeline (que usa rutas relativas tipo "proyectos", "referencias"): si esta
# app se arranca desde otro directorio (otra ventana de Terminal, otro
# lanzador), sin esto leeria/escribiria en la carpeta equivocada y mezclaria
# sus proyectos con los de otra copia de la app -- justo lo que esta
# aislacion por carpeta deberia impedir.
os.chdir(pathlib.Path(__file__).resolve().parent)

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response

import pipeline as P

AQUI = pathlib.Path(__file__).parent
app = FastAPI(title="Pipeline de video")

# --------------------------------------------------------- trabajo en curso
TRABAJO = {"activo": False, "tarea": "", "hecho": 0, "total": 0,
           "inicio": 0.0, "log": []}
_lock = threading.Lock()


def _log(msg):
    with _lock:
        TRABAJO["log"].append(str(msg))
        del TRABAJO["log"][:-300]
    print(msg, flush=True)


def _log_contador(msg):
    if str(msg).startswith(("imagen ", "audio ", "clip ", "  prompts ", "  rotulos ")):
        TRABAJO["hecho"] += 1
    _log(msg)


def _lanzar(nombre, fn, total=0):
    if TRABAJO["activo"]:
        return False
    P.parar(False)
    TRABAJO.update({"activo": True, "tarea": nombre, "hecho": 0,
                    "total": total, "inicio": time.time(), "log": []})

    def correr():
        try:
            fn()
        except Exception:
            _log("ERROR:\n" + traceback.format_exc())
        finally:
            TRABAJO["activo"] = False
            _log(f"— {nombre}: terminado —")

    threading.Thread(target=correr, daemon=True).start()
    return True


def _ocupado():
    return JSONResponse({"error": "Ya hay un trabajo en curso"}, status_code=409)


# --------------------------------------------------------------------- api
@app.get("/")
def index():
    # sin cache: index.html cambia a menudo durante el desarrollo, y el
    # navegador cacheando una version vieja hacia que un arreglo ya aplicado
    # pareciera que "no funciona" (el usuario seguia viendo el JS de antes)
    return FileResponse(AQUI / "index.html", headers={
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Pragma": "no-cache",
    })


@app.get("/api/estado")
def estado():
    escenas = P.cargar_escenas()
    visto = set()
    con_img = con_aud = 0
    for e in escenas:
        fid = e.get("frase_id", e["id"])
        e["tiene_img"] = P.ruta_img(e["id"]).exists()
        e["tiene_audio"] = P.ruta_audio(fid).exists()
        e["primero_frase"] = fid not in visto
        visto.add(fid)
        e["dur"] = round(P.duracion_estimada(e), 1)
        con_img += e["tiene_img"]
    frases = P._frases_unicas(escenas)
    con_aud = sum(1 for f, _ in frases if P.ruta_audio(f).exists())

    probs = P.comprobar(escenas)
    por_id = {}
    for p in probs:
        por_id.setdefault(p["id"], []).append(p)
    for e in escenas:
        avisos = [p["texto"] for p in por_id.get(e["id"], [])]
        ch = e.get("check") or {}
        if ch and not (ch.get("texto_ok", True) and ch.get("estilo_ok", True)):
            avisos.append("la imagen no pasó el control: " + (ch.get("nota") or ""))
        e["avisos"] = avisos

    t = dict(TRABAJO)
    if t["activo"] and t["total"] and t["hecho"]:
        pasado = time.time() - t["inicio"]
        t["restante"] = int(pasado / t["hecho"] * (t["total"] - t["hecho"]))
    precio = P.COSTE_IMG.get(P.MODELO_IMG, 0.05)
    return {
        "escenas": escenas,
        "trabajo": t,
        "video": P.VIDEO.exists(),
        "tipos": P.TIPOS_PLANO,
        "proyecto": P.proyecto_activo(),
        "titulo": P.cargar_titulo(),
        "refs": [p.name for p in P.archivos_ref()],
        "resumen": {
            "planos": len(escenas), "frases": len(frases),
            "con_img": con_img, "con_audio": con_aud,
            "segundos": round(sum(P.duracion_estimada(e) for e in escenas)),
            "modelo_img": P.MODELO_IMG, "precio_img": precio,
            "problemas": sum(1 for e in escenas if e["avisos"]),
            "errores": sum(1 for p in probs if p["nivel"] == "error"),
            "gasto_total": P.cargar_gasto().get("total", 0.0),
        },
    }


@app.get("/api/gasto")
def leer_gasto():
    return P.cargar_gasto()


# ------------------------------------------------------------------ ideas
@app.get("/api/ideas")
def leer_ideas():
    return {"ideas": P.listar_ideas(), "estados": P.ESTADOS_IDEA}


@app.post("/api/ideas")
async def crear_idea(req: Request):
    d = await req.json()
    titulo = (d.get("titulo") or "").strip()
    if not titulo:
        return JSONResponse({"error": "falta el titulo"}, status_code=400)
    idea = P.anadir_idea(titulo, d.get("resumen", "").strip(), d.get("nicho", "").strip(),
                          d.get("notas", "").strip(), d.get("estado", "idea"))
    return {"ok": True, "idea": idea}


@app.post("/api/ideas/{idea_id}")
async def editar_idea(idea_id: int, req: Request):
    idea = P.actualizar_idea(idea_id, await req.json())
    if not idea:
        return JSONResponse({"error": "no existe"}, status_code=404)
    return {"ok": True, "idea": idea}


@app.delete("/api/ideas/{idea_id}")
def eliminar_idea(idea_id: int):
    P.borrar_idea(idea_id)
    return {"ok": True}


# ------------------------------------------------- referencia de formato
@app.post("/api/referencia_video")
async def analizar_referencia(req: Request):
    d = await req.json()
    url = (d.get("url") or "").strip()
    if not url:
        return JSONResponse({"error": "falta la URL"}, status_code=400)
    transcripcion = (d.get("transcripcion") or "").strip()

    def hacer():
        analisis = P.analizar_video_referencia(url, transcripcion, log=_log)
        P.proponer_ideas_similares(analisis, log=_log)

    if not _lanzar("Analizando video de referencia", hacer, 1):
        return _ocupado()
    return {"ok": True}


GUION_PENDIENTE = P.PROYECTOS / "guion_pendiente.txt"


@app.post("/api/escribir_guion")
async def escribir_guion_endpoint(req: Request):
    """Escribe el guion COMPLETO a partir de una idea ya propuesta (por su
    id), ajustandose a su objetivo de duracion/ritmo si lo trae. Es una sola
    llamada de texto, centimos -- no genera imagenes ni voz. Se lanza en
    segundo plano como el resto de la app (mismo patron de _lanzar +
    /api/estado): el resultado se guarda aparte y se lee con el GET de abajo
    cuando el trabajo termine."""
    idea_id = (await req.json()).get("idea_id")
    idea = next((i for i in P.listar_ideas() if i["id"] == idea_id), None)
    if not idea:
        return JSONResponse({"error": "no existe esa idea"}, status_code=404)

    def hacer():
        guion = P.escribir_guion_desde_idea(idea, log=_log)
        P.PROYECTOS.mkdir(exist_ok=True)
        GUION_PENDIENTE.write_text(guion or "", encoding="utf-8")

    if not _lanzar(f"Escribiendo guion: {idea['titulo']}", hacer, 1):
        return _ocupado()
    return {"ok": True}


@app.get("/api/escribir_guion")
def leer_guion_pendiente():
    if not GUION_PENDIENTE.exists():
        return JSONResponse({"error": "aun no generado"}, status_code=404)
    return {"guion": GUION_PENDIENTE.read_text(encoding="utf-8")}


@app.get("/api/presupuesto")
def leer_presupuesto():
    """Chequeo previo: caracteres de ElevenLabs y gasto total de la app frente
    a tu limite, para saber si generar este video te deja a medias o no."""
    return P.comprobar_presupuesto(P.cargar_escenas())


@app.post("/api/presupuesto")
async def guardar_presupuesto(req: Request):
    val = (await req.json()).get("limite_eur")
    if val is None:
        P.fijar_presupuesto(None)
        return {"ok": True}
    try:
        eur = float(val)
    except (TypeError, ValueError):
        return JSONResponse({"error": "limite invalido"}, status_code=400)
    P.fijar_presupuesto(eur)
    return {"ok": True}


@app.post("/api/parar")
def detener():
    P.parar(True)
    _log("parando...")
    return {"ok": True}


@app.post("/api/titulo")
async def poner_titulo(req: Request):
    """El nombre que TU le pones al proyecto para verlo en la app -- no toca
    el nombre de la carpeta (ese sigue siendo el slug tecnico del guion, para
    no romper nada que dependa de el)."""
    titulo = (await req.json()).get("titulo", "").strip()
    P.guardar_titulo(titulo)
    return {"ok": True, "titulo": P.cargar_titulo()}


@app.post("/api/guion")
async def subir_guion(req: Request):
    """Reescribe el guion del proyecto ACTIVO y regenera su storyboard, en la
    misma carpeta (para retocar texto sin perder lo ya generado por numero).
    Para un video distinto, usa /api/proyectos: asi nunca se mezclan."""
    texto = (await req.json()).get("texto", "").strip()
    if not texto:
        return JSONResponse({"error": "guion vacio"}, status_code=400)
    P.versionar(_log)
    P.GUION.write_text(texto, encoding="utf-8")
    n = len(P.desglosar(texto))
    pasos = -(-n // P.LOTE) + -(-n // 40)     # lotes de prompts + lotes de rotulos
    if not _lanzar("Analizando el guion",
                   lambda: P.generar_escenas(texto, _log_contador), pasos):
        return _ocupado()
    return {"ok": True}


@app.get("/api/guion")
def leer_guion():
    return {"texto": P.GUION.read_text(encoding="utf-8") if P.GUION.exists() else ""}


# ---------------------------------------------------------------- proyectos
@app.get("/api/proyectos")
def listar_proyectos():
    return {"proyectos": P.listar_proyectos(), "activo": P.proyecto_activo()}


@app.post("/api/proyectos")
async def nuevo_proyecto(req: Request):
    """Video nuevo y aislado: carpeta propia, nunca hereda imagenes/audio de otro."""
    if TRABAJO["activo"]:
        return _ocupado()
    texto = (await req.json()).get("texto", "").strip()
    if not texto:
        return JSONResponse({"error": "guion vacio"}, status_code=400)
    nombre = P.crear_proyecto(texto)
    n = len(P.desglosar(texto))
    pasos = -(-n // P.LOTE) + -(-n // 40)
    _lanzar("Analizando el guion", lambda: P.generar_escenas(texto, _log_contador), pasos)
    return {"ok": True, "proyecto": nombre}


@app.post("/api/proyectos/activar")
async def activar_proyecto(req: Request):
    if TRABAJO["activo"]:
        return _ocupado()
    nombre = (await req.json()).get("nombre", "")
    if not (P.PROYECTOS / nombre).is_dir():
        return JSONResponse({"error": "proyecto no encontrado"}, status_code=404)
    P.activar_proyecto(nombre)
    return {"ok": True}


@app.post("/api/escena/{eid}")
async def editar(eid: int, req: Request):
    cambios = await req.json()
    escenas = P.cargar_escenas()
    for e in escenas:
        if e["id"] == eid:
            for k in ("plano", "rotulo", "prompt"):
                if k in cambios:
                    e[k] = cambios[k]
            P.guardar_escenas(escenas)
            return {"ok": True}
    return JSONResponse({"error": "no existe"}, status_code=404)


def _gate_presupuesto(escenas, forzar):
    """Bloquea el gasto si te pasarias del limite que TU fijaste en
    Presupuesto, salvo que mandes forzar=true a proposito. Sin esto, el
    limite era solo un aviso que habia que ir a mirar a mano; ahora nadie
    (ni yo ni la app) puede saltarselo sin que tu lo pidas explicitamente."""
    if forzar:
        return None
    chk = P.comprobar_presupuesto(escenas)
    limite = chk.get("limite_eur")
    proyectado = chk.get("gasto_app_proyectado")
    if limite is not None and proyectado is not None and proyectado > limite:
        return JSONResponse({
            "error": "limite_presupuesto",
            "mensaje": (f"Esto dejaria el gasto total de la app en {proyectado:.2f} €, "
                        f"por encima de tu limite de {limite:.2f} €. Sube el limite en "
                        f"Presupuesto o repite la llamada con forzar=true si quieres "
                        f"seguir de todas formas."),
            "proyectado": proyectado, "limite": limite,
        }, status_code=409)
    return None


@app.post("/api/imagenes")
def _gate_referencias(forzar):
    """Bloquea generar imagenes si falta subir la hoja de referencia de
    algun elemento de la biblia. Las hojas ya NO se generan solas (ver
    generar_escenas): las sube la persona en el panel Referencias, con la
    imagen que ella misma elija, no una que la app se invente. Con
    forzar=true se puede saltar (por si de verdad se quiere generar sin
    referencia para algo puntual)."""
    if forzar:
        return None
    biblia = P.cargar_biblia()
    faltan = [e["nombre"] for e in biblia.get("elementos", [])
              if not (P.REF_PROYECTO / f"{P.slug(e['nombre'], 3)}.jpeg").exists()]
    if faltan:
        return JSONResponse({
            "error": "faltan_referencias",
            "mensaje": (f"Faltan imagenes de referencia para: {', '.join(faltan)}. "
                        f"Sube una por cada uno en el panel Referencias antes de "
                        f"generar, o repite la llamada con forzar=true si quieres "
                        f"generar sin ellas de todas formas."),
            "faltan": faltan,
        }, status_code=409)
    return None


async def gen_imagenes(req: Request):
    """Sin cuerpo: las que falten. Con {"ids":[...]}: esas, rehaciendolas.
    Con {"forzar":true}: se salta el limite de presupuesto Y el aviso de
    referencias que falten.
    Con {"verificar":false}: no comprueba ni reintenta cada imagen, genera una
    vez y sigue adelante (mas barato y rapido, a cambio de mas posibilidad de
    que alguna salga mal y haya que regenerarla suelta luego)."""
    try:
        body = await req.json()
    except Exception:
        body = {}
    ids = body.get("ids")
    verificar = body.get("verificar", True)
    escenas = P.cargar_escenas()
    bloqueo = _gate_presupuesto(escenas, body.get("forzar"))
    if bloqueo:
        return bloqueo
    bloqueo = _gate_referencias(body.get("forzar"))
    if bloqueo:
        return bloqueo
    if ids:
        total, nombre = len(ids), f"Regenerando {len(ids)} imagen(es)"
    else:
        total = sum(1 for e in escenas if not P.ruta_img(e["id"]).exists())
        nombre = "Generando imagenes"
    if not _lanzar(nombre,
                   lambda: P.generar_imagenes(escenas, ids=ids, verificar=verificar,
                                              log=_log_contador), total):
        return _ocupado()
    return {"ok": True, "total": total}


@app.post("/api/verificar")
async def verificar_imagenes(req: Request):
    """Audita imagenes YA generadas (sin volver a generarlas) y guarda el
    resultado en 'check' de cada plano. Sin cuerpo: todas las que tengan
    imagen. Con {"ids":[...]}: solo esas."""
    try:
        ids = (await req.json()).get("ids")
    except Exception:
        ids = None
    escenas = P.cargar_escenas()
    total = len(ids) if ids else sum(1 for e in escenas if P.ruta_img(e["id"]).exists())
    if not _lanzar("Auditando imagenes",
                   lambda: P.verificar_solo(escenas, ids=ids, log=_log_contador), total):
        return _ocupado()
    return {"ok": True, "total": total}


@app.get("/api/referencias")
def listar_referencias():
    """Los elementos de la biblia visual de este proyecto y si ya tienen
    hoja de referencia (generada o subida a mano)."""
    biblia = P.cargar_biblia()
    paleta = biblia.get("paleta", [])
    out = []
    for elem in biblia.get("elementos", []):
        archivo = f"{P.slug(elem['nombre'], 3)}.jpeg"
        existe = (P.REF_PROYECTO / archivo).exists() if P.REF_PROYECTO else False
        out.append({"nombre": elem["nombre"], "descripcion": elem["descripcion"],
                    "archivo": archivo, "existe": existe,
                    "prompt_referencia": P.prompt_referencia_humano(elem["descripcion"], paleta)})
    return {"elementos": out, "paleta": paleta}


@app.get("/media/referencia/{archivo}")
def media_referencia(archivo: str):
    if "/" in archivo or "\\" in archivo or ".." in archivo:
        return Response(status_code=400)
    p = P.REF_PROYECTO / archivo
    if not p.exists():
        return Response(status_code=404)
    return FileResponse(p, headers={"Cache-Control": "no-store"})


@app.post("/api/referencia_subida")
async def subir_referencia(req: Request):
    """Sube una imagen propia (ej. una foto real de tu gato) como hoja de
    referencia de un elemento, sustituyendo a la generada automaticamente."""
    body = await req.json()
    nombre = (body.get("nombre") or "").strip()
    datos_b64 = (body.get("datos") or "")
    if not nombre or not datos_b64:
        return JSONResponse({"error": "falta nombre o imagen"}, status_code=400)
    import base64
    try:
        datos = base64.b64decode(datos_b64.split(",")[-1])
    except Exception:
        return JSONResponse({"error": "imagen invalida"}, status_code=400)
    destino = P.REF_PROYECTO / f"{P.slug(nombre, 3)}.jpeg"
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_bytes(datos)
    return {"ok": True, "archivo": destino.name}


@app.post("/api/referencia_personaje")
async def ref_personaje(req: Request):
    """Regenera a mano la hoja de referencia de UN elemento de la biblia visual
    de ESTE proyecto (normalmente se generan solas al crear el storyboard)."""
    body = await req.json()
    nombre = (body.get("nombre") or "elemento").strip()
    descripcion = (body.get("descripcion") or "").strip()
    if not descripcion:
        return JSONResponse({"error": "falta descripcion"}, status_code=400)
    destino = P.REF_PROYECTO / f"{P.slug(nombre, 3)}.jpeg"
    paleta = P.cargar_biblia().get("paleta")
    if not _lanzar(f"Hoja de referencia: {nombre}",
                   lambda: P.generar_hoja_referencia(descripcion, destino, paleta, log=_log), 1):
        return _ocupado()
    return {"ok": True, "archivo": str(destino)}


@app.post("/api/voz")
async def gen_voz(req: Request):
    try:
        body = await req.json()
    except Exception:
        body = {}
    ids = body.get("ids")
    escenas = P.cargar_escenas()
    bloqueo = _gate_presupuesto(escenas, body.get("forzar"))
    if bloqueo:
        return bloqueo
    if ids:
        eid = ids[0]
        total, nombre = 1, f"Regenerando audio del plano {eid}"
        fn = lambda: P.generar_voces(escenas, solo=eid, rehacer=True, log=_log_contador)
    else:
        total = sum(1 for f, _ in P._frases_unicas(escenas) if not P.ruta_audio(f).exists())
        nombre = "Generando voz"
        fn = lambda: P.generar_voces(escenas, log=_log_contador)
    if not _lanzar(nombre, fn, total):
        return _ocupado()
    return {"ok": True, "total": total}


@app.get("/api/comprobar")
def comprobar():
    """Revision gratis del storyboard: numeracion, prompts, rotulos, duraciones."""
    return {"problemas": P.comprobar(P.cargar_escenas())}


@app.post("/api/montar")
async def montar(req: Request):
    try:
        forzar = bool((await req.json()).get("forzar"))
    except Exception:
        forzar = False
    escenas = P.cargar_escenas()
    if not _lanzar("Montando el video",
                   lambda: P.montar_video(escenas, forzar, _log_contador), len(escenas)):
        return _ocupado()
    return {"ok": True}


@app.get("/api/seo")
def leer_seo():
    d = P.cargar_seo()
    if not d:
        return JSONResponse({"error": "aun no generado"}, status_code=404)
    d["subtitulos"] = P.SUBTITULOS.exists()
    return d


@app.post("/api/seo")
def gen_seo():
    escenas = P.cargar_escenas()
    if not escenas:
        return JSONResponse({"error": "no hay storyboard"}, status_code=400)
    if not _lanzar("Generando SEO", lambda: P.generar_seo(escenas, log=_log), 1):
        return _ocupado()
    return {"ok": True}


@app.post("/api/subtitulos")
def gen_subtitulos():
    escenas = P.cargar_escenas()
    if not _lanzar("Generando subtitulos", lambda: P.generar_subtitulos(escenas, log=_log), 1):
        return _ocupado()
    return {"ok": True}


# --------------------------------------------------------- puntuacion SEO
@app.post("/api/puntuar_seo")
def gen_puntuacion_seo():
    seo = P.cargar_seo()
    if not seo:
        return JSONResponse({"error": "genera el SEO primero"}, status_code=400)
    if not _lanzar("Puntuando SEO", lambda: P.puntuar_seo(seo, log=_log), 1):
        return _ocupado()
    return {"ok": True}


@app.get("/api/puntuar_seo")
def leer_puntuacion_seo():
    p = P.OUT / "puntuacion_seo.json"
    if not p.exists():
        return JSONResponse({"error": "aun no puntuado"}, status_code=404)
    return json.loads(p.read_text(encoding="utf-8"))


# ------------------------------------------------------------- miniatura
@app.post("/api/miniatura")
async def gen_miniatura(req: Request):
    try:
        forzar = bool((await req.json()).get("forzar"))
    except Exception:
        forzar = False
    escenas = P.cargar_escenas()
    if not escenas:
        return JSONResponse({"error": "no hay storyboard"}, status_code=400)
    bloqueo = _gate_presupuesto(escenas, forzar)
    if bloqueo:
        return bloqueo
    guion = P.GUION.read_text(encoding="utf-8") if P.GUION.exists() else ""

    def hacer():
        destinos = P.generar_miniatura(guion, n=2, log=_log)
        for d in destinos:
            r = P.puntuar_miniatura(d, log=_log)
            _log(f"  {d.name}: nota {r.get('nota')} -- {r.get('sugerencia', '')}")
            (d.with_suffix(".json")).write_text(
                json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")

    if not _lanzar("Generando miniaturas", hacer, 2):
        return _ocupado()
    return {"ok": True}


@app.get("/api/miniatura")
def listar_miniaturas():
    out = []
    if P.OUT and P.OUT.is_dir():
        for img in sorted(P.OUT.glob("miniatura_*.jpeg")):
            puntuacion_path = img.with_suffix(".json")
            puntuacion = (json.loads(puntuacion_path.read_text(encoding="utf-8"))
                          if puntuacion_path.exists() else None)
            out.append({"archivo": img.name, "puntuacion": puntuacion})
    return {"miniaturas": out}


@app.get("/media/miniatura/{archivo}")
def media_miniatura(archivo: str):
    if "/" in archivo or "\\" in archivo or ".." in archivo:
        return Response(status_code=400)
    p = P.OUT / archivo
    if not p.exists():
        return Response(status_code=404)
    return FileResponse(p)


# ------------------------------------------------------------------- media
@app.get("/media/img/{eid}")
def media_img(eid: int):
    p = P.ruta_img(eid)
    if not p.exists():
        return Response(status_code=404)
    return FileResponse(p, headers={"Cache-Control": "no-store"})


@app.get("/media/audio/{eid}")
def media_audio(eid: int):
    e = next((x for x in P.cargar_escenas() if x["id"] == eid), None)
    p = P.ruta_audio(e.get("frase_id", eid)) if e else P.ruta_audio(eid)
    if not p.exists():
        return Response(status_code=404)
    return FileResponse(p, headers={"Cache-Control": "no-store"})


@app.get("/media/video")
def media_video():
    if not P.VIDEO.exists():
        return Response(status_code=404)
    return FileResponse(P.VIDEO)


@app.get("/media/subtitulos")
def media_subtitulos():
    if not P.SUBTITULOS.exists():
        return Response(status_code=404)
    return FileResponse(P.SUBTITULOS, media_type="text/plain", filename="subtitulos.srt")


if __name__ == "__main__":
    import uvicorn
    print("\n  Abre  http://127.0.0.1:8002  en el navegador\n")
    uvicorn.run(app, host="127.0.0.1", port=8002, log_level="warning")