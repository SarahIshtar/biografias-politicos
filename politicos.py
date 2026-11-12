import pandas as pd
from playwright.sync_api import sync_playwright
from tqdm import tqdm
import json
import os
import time
import random
import argparse
import urllib.parse
import re


## Constantes de configuración
CSV_ENTRADA = "politicos.csv"
CSV_SALIDA = "politicos_wiki.csv"
CHECKPOINT = "checkpoint.json"
PAUSA_MIN = 1.5
PAUSA_MAX = 4.0
PAUSA_LARGA_MIN = 30.0
PAUSA_LARGA_MAX = 90.0
CADA_N_MIN = 45
CADA_N_MAX = 55
GUARDAR_CADA = 10
USER_AGENT = "ProyectoPoliticos/1.0 (uso academico)"


##Ordenar CSV por activos y sueldo
def cargar_y_ordenar_csv():
    df = pd.read_csv(CSV_ENTRADA)
    df["salario_num"] = (
        df["salarioAnual"]
        .str.replace(" €", "", regex=False)
        .str.replace(".", "", regex=False)
        .str.replace(",", ".", regex=False)
        .astype(float)
    )

    df = df.sort_values(by=["activo", "salario_num"], ascending=[False, False])
    return df


##Buscar página de Wikipedia para un político
##Devuelve (url, metodo): metodo es "directo", "busqueda" o None
def buscar_pagina(page, nombre):
    url = "https://es.wikipedia.org/w/index.php?search=" + urllib.parse.quote(nombre)
    try:
        page.goto(url, timeout=20000)
    except Exception:
        return "error", None

    # Caso 1: Wikipedia redirige directamente al artículo
    if "/wiki/" in page.url and "search=" not in page.url:
        return page.url, "directo"

    # Caso 2: lista de resultados -> aceptar el primero solo si
    # todas las palabras de su título están en el nombre del CSV
    resultados = page.locator(".mw-search-result-heading a")
    if resultados.count() == 0:
        return None, None
    primero = resultados.first
    titulo = primero.inner_text()

    palabras_titulo = set(titulo.lower().split())
    palabras_nombre = set(nombre.lower().split())
    if len(palabras_titulo) >= 2 and palabras_titulo <= palabras_nombre:
        return "https://es.wikipedia.org" + primero.get_attribute("href"), "busqueda"

    return None, None


##Ir al artículo si hace falta
def extraer_datos(page, url):
    if page.url != url:
        try:
            page.goto(url, timeout=20000)
        except Exception:
            return {"descripcion": None, "url_imagen": None, "estado": "error"}

    ##Primer párrafo y imagen
    descripcion = None
    parrafos = page.locator(".mw-parser-output > section > p, .mw-parser-output > p").all()
    for p in parrafos:
        texto = p.inner_text().strip()
        if texto:
            descripcion = re.sub(r"\[\d+\]", "", texto)
            break

    url_imagen = None
    meta = page.locator('meta[property="og:image"]')
    if meta.count() > 0:
        url_imagen = meta.first.get_attribute("content").split("?")[0]

    ##Comprobar si es político
    url_wiki = url
    canonica = page.locator('link[rel="canonical"]')
    if canonica.count() > 0:
        url_wiki = canonica.first.get_attribute("href")

    palabras_clave = ["polític", "diputad", "senador", "alcald", "concejal", "ministr",
                      "parlament", "president", "secretari", "director", "consejer",
                      "funcionari", "gobernador", "comisari", " rey ","magistrad", "jurista", 
                      "juez", "fiscal", "diplomátic", "embajador", "almirante", "militar", "jefe de estado"]
    texto_min = (descripcion or "").lower()
    if any(palabra in texto_min for palabra in palabras_clave):
        estado = "ok"
    else:
        estado = "no_politico"

    return {"descripcion": descripcion, "url_imagen": url_imagen, "url_wiki": url_wiki, "estado": estado}


##Checkpoint
###Cargarlo desde archivo JSON
def cargar_checkpoint(ruta):
    if os.path.exists(ruta):
        with open(ruta, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

###Guardar checkpoint en archivo JSON
def guardar_checkpoint(datos, ruta):
    temporal = ruta + ".tmp"
    with open(temporal, "w", encoding="utf-8") as f:
        json.dump(datos, f, ensure_ascii=False, indent=2)
    os.replace(temporal, ruta)


#Procesar un político 
def procesar_politico(page, nombre):
    url, metodo = buscar_pagina(page, nombre)
    if url == "error":
        return None
    if url is None:
        registro = {"estado": "no_encontrado"}
    else:
        registro = extraer_datos(page, url)
        if registro["estado"] == "error":
            return None
    registro["nombre"] = nombre
    registro["metodo"] = metodo
    return registro


def ejecutar(df, ruta_checkpoint):
    checkpoint = cargar_checkpoint(ruta_checkpoint)
    pendientes = [i for i in df.index if str(i) not in checkpoint]
    print(f"Total: {len(df)} | Ya hechos: {len(df) - len(pendientes)} | Pendientes: {len(pendientes)}")

    peticiones = 0
    siguiente_pausa_larga = random.randint(CADA_N_MIN, CADA_N_MAX)
    nuevos = 0
    with sync_playwright() as p:
        navegador = p.chromium.launch(headless=True)
        contexto = navegador.new_context(user_agent=USER_AGENT)
        pagina = contexto.new_page()

        try:
            for indice in tqdm(pendientes, desc="Políticos", unit="pol"):
                nombre = df.at[indice, "nombre"]
                registro = procesar_politico(pagina, nombre)

                if registro is None:
                    tqdm.write(f"Error con {nombre}, se reintentará en otra ejecución")
                else:
                    checkpoint[str(indice)] = registro
                    nuevos += 1
                    if nuevos % GUARDAR_CADA == 0:
                        guardar_checkpoint(checkpoint, ruta_checkpoint)
                peticiones += 1
                if peticiones >= siguiente_pausa_larga:
                    espera = random.uniform(PAUSA_LARGA_MIN, PAUSA_LARGA_MAX)
                    tqdm.write(f"Pausa larga de {espera:.0f} segundos...")
                    time.sleep(espera)
                    peticiones = 0
                    siguiente_pausa_larga = random.randint(CADA_N_MIN, CADA_N_MAX)
                else:
                    time.sleep(random.uniform(PAUSA_MIN, PAUSA_MAX))
        except KeyboardInterrupt:
            tqdm.write("Interrumpido. Guardando lo hecho hasta ahora...")
        finally:
            guardar_checkpoint(checkpoint, ruta_checkpoint)
            tqdm.write(f"Checkpoint guardado: {len(checkpoint)} registros")
            navegador.close()

    return checkpoint


##Exportar a CSV
def exportar_csv(df, checkpoint):
    resultados = pd.DataFrame.from_dict(checkpoint, orient="index")
    resultados.index = resultados.index.astype(int)
    resultados = resultados.reindex(columns=["descripcion", "url_imagen", "url_wiki", "metodo", "estado"])
    resultados = resultados.rename(columns={"metodo": "metodo_wiki", "estado": "estado_wiki"})

    final = df.drop(columns=["salario_num"]).join(resultados)
    final.to_csv(CSV_SALIDA, index=False, encoding="utf-8-sig")
    print(f"CSV guardado en {CSV_SALIDA}: {len(final)} filas, {final['estado_wiki'].eq('ok').sum()} con datos de Wikipedia")


##Argumentos de terminal
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Descarga de Wikipedia el primer párrafo y la imagen de cada político")
    parser.add_argument("--limite", type=int, default=None, help="Procesar solo los N primeros")
    parser.add_argument("--solo-activos", action="store_true", help="Procesar solo los activos")
    parser.add_argument("--salario-minimo", type=float, default=None, help="Procesar solo los que cobran al menos esta cantidad (en euros)")
    parser.add_argument("--checkpoint", default=CHECKPOINT, help="Archivo de checkpoint a usar")
    args = parser.parse_args()

    df_completo = cargar_y_ordenar_csv()
    df = df_completo
    if args.solo_activos:
        df = df[df["activo"]]
    if args.salario_minimo is not None:
        df = df[df["salario_num"] >= args.salario_minimo]
    if args.limite:
        df = df.head(args.limite)

    checkpoint = ejecutar(df, args.checkpoint)
    exportar_csv(df_completo, checkpoint)