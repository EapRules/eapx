# eapx — diseño

Creado y mantenido por **EapRules**.

Extractor de first-boot para ports Android→Linux en handhelds. Objetivo concreto:
R36S / ArkOS, aarch64, glibc 2.30, Python 3.7.5, SD lenta, 1 GB de RAM, 640×480.

Este documento es la especificación desde la que se escribe el código. No hay código
heredado: el diseño se destiló leyendo un extractor existente, y las decisiones de
abajo se toman a partir de ese entendimiento más una auditoría de sus fallas.

---

## 1. Qué resuelve

El usuario deja el APK/XAPK/OBB del juego en el directorio del port. En el primer
arranque, eapx descubre el paquete **por contenido y no por nombre**, extrae lo que la
receta declara, valida, y publica. Las corridas siguientes salen en milisegundos.

No es un instalador genérico. Es la pieza que va adentro del zip de un port y corre
antes del loader:

```bash
./eapx.sh || exit 1
exec ./loader "$GAMEDIR"
```

## 2. Principios

1. **El nombre del archivo es metadata de log, nunca identidad.** Todo se decide por
   contenido.
2. **Nada toca el directorio vivo hasta que todo está validado.** Se construye un árbol
   completo aparte y se publica por rename.
3. **Ante ambigüedad, fallar.** Dos paquetes distintos que ambos matchean es un error
   del usuario con arreglo de diez segundos. Adivinar mal es un juego roto sin
   diagnóstico.
4. **Registrar la intención antes de actuar, nunca el hecho después.** Ver §4.
5. **Nunca borrar en el directorio vivo por mera existencia.** Solo por identidad de
   contenido verificada.
6. **Leer cada byte una sola vez.** En una SD de 20 MB/s cada pasada extra sobre 2 GB
   son 100 segundos de reloj.

## 3. Flujo

| # | Fase | Garantía al salir |
|---|---|---|
| 0 | Recuperación | No hay transacciones a medio aplicar. Precondición de todo lo demás. |
| 1 | Fast-path | Si el marker vigente valida barato, se sale sin abrir un solo zip. |
| 2 | Adopción | Si el árbol ya cumple el contrato completo, se adopta sin necesitar el APK. |
| 3 | Descubrimiento | Se conoce el conjunto de candidatos, clasificados por contenido. |
| 4 | Plan | Una ABI elegida, una lista de items sin colisiones, un fingerprint determinístico. |
| 5 | Extracción | Cada item existe en el stage con tamaño y hash correctos. Reanudable. |
| 6 | Hooks | Post-procesos corridos, con timeout, con checkpoints validados. |
| 7 | Validación | El **stage** satisface el contrato completo. Nada sale sin pasar por acá. |
| 8 | Commit | Publicación transaccional. Ver §4. |

Todo bajo un lock a nivel de **game_dir** (no de receta): dos recetas distintas sobre el
mismo directorio pueden solaparse en sus raíces de commit, y serializar cuesta nada.

### 3.1 Adopción, y sus límites

Si el juego está instalado pero el marker se perdió —el usuario lo borró, movió la SD,
o copió el port ya instalado desde otra tarjeta— pedirle el paquete de nuevo es hacerle
repetir veinte minutos para nada.

Pero sin el paquete no tenemos hashes de referencia contra qué comparar. Lo único
disponible es preguntarle a la propia receta, vía su bloque `validate`, si el árbol
tiene forma de instalación terminada. De ahí salen dos decisiones:

- **Una receta sin `validate` no puede adoptar.** Adoptaría cualquier cosa. Se rechaza
  con ese motivo explícito en el log, en vez de aceptar y mentir.
- **El marker sintético guarda rutas y tamaños, no hashes.** Afirmar conocimiento
  byte a byte de datos que nunca vimos llegar sería falso, y calcularlos igual costaría
  una lectura completa del payload sin comprar ninguna garantía real. `verify` lo dice
  cuando corre sobre un marker adoptado: *"sizes checked, content unverified"*.

`--no-adopt` lo desactiva.

## 4. Modelo transaccional

El workspace vive en `<game_dir>/.eapx/<recipe_id>/`, dentro del game_dir a propósito:
garantiza mismo filesystem y por lo tanto que todo rename sea atómico y no dé EXDEV.
El preflight verifica `st_dev` igual entre workspace, game_dir y cada destino existente,
y falla temprano si algún destino es un punto de montaje.

```
.eapx/<id>/
  stage/      árbol nuevo, construido y validado offline
  backup/     pre-imagen: lo que había en el game_dir antes de pisarlo
  cache/      APKs internos expandidos de bundles (solo acelerador)
  journal     estado de la transacción
  lock        flock a nivel game_dir
  progress    estado para la UI
```

### 4.1 Journal de intenciones

Esta es la diferencia central con el diseño del que partimos, y sale directo de la
auditoría: **el journal describe lo que está por pasar, no lo que pasó.**

Por cada raíz de commit se registra, antes de tocar nada:

```
{ path, state, digest_new, digest_old }
```

- `state` avanza `PENDING → BACKING_UP → BACKED_UP → INSTALLING → INSTALLED`.
- `digest_new` es el hash del contenido que vamos a instalar (ya lo tenemos: viene del
  plan).
- `digest_old` es el hash de lo que había, calculado antes de moverlo.

La transición se escribe **antes** de la operación que describe. Un journal adelantado
es entonces inofensivo: la recuperación sabe "esto puede haber pasado o no" y **resuelve
por contenido**, no por flag. Con eso desaparecen de un saque cuatro fallas distintas
del diseño original, todas causadas por la misma inversión.

### 4.2 Recuperación

Idempotente por construcción, porque toda decisión se toma comparando el hash de lo que
está en disco contra `digest_new` / `digest_old`:

| Lo que hay en el destino | Acción |
|---|---|
| Coincide con `digest_new` | Ya se instaló. Devolverlo al stage si hay que revertir; dejarlo si hay que completar. |
| Coincide con `digest_old` | Es el original. No tocar. |
| No existe | Restaurar del backup si está; si no, marcar el path como pendiente. |
| No coincide con ninguno | **Parar.** Algo externo lo modificó. Nunca borrar. |

La última fila es la regla 5 en acción. El diseño anterior, en esa situación, borraba.

**El rollback también persiste su progreso.** Un rollback interrumpido y reejecutado no
puede destruir lo que ya restauró — que es exactamente lo que pasaba antes.

### 4.3 Punto de no retorno

Uno solo: el `os.replace` del marker. Antes de esa línea, cualquier corte lleva a
rollback; después, a completar. Pero con dos condiciones que el diseño original no
cumplía:

1. **Antes de escribir el marker se fsyncean todos los directorios padre tocados**, no
   solo la raíz del game_dir. Si el marker se hace durable antes que los renames que
   certifica, la recuperación cree que la transacción se publicó y borra el backup —
   perdiendo original y payload juntos.
2. **El backup es lo último que se borra**, y solo después de una verificación positiva
   del árbol resultante. Nunca por confianza en un flag.

### 4.4 Capacidad de fsync

`fsync` de directorio se detecta una vez al abrir el workspace y **se loguea**. En exFAT
o vfat —escenario realista en una SD de handheld— puede no estar soportado, y en ese
caso el usuario merece saber que la transacción resiste un kill pero no un corte de luz.
Tragarse el error en silencio, como hacía el original, es peor que no tener la garantía:
es creer que la tenés.

### 4.5 Granularidad del commit

El commit publica raíces completas por rename. Eso significa que **todo lo que vive bajo
una raíz y no fue producido por el extractor desaparece**: saves, configuración, mods.

Decisión: el preflight enumera lo que hay bajo cada raíz y compara contra el plan. Si
encuentra archivos ajenos, **falla con la lista** en vez de destruirlos en silencio. La
receta puede declarar `"exclusive": true` en una raíz para autorizar el reemplazo total.
El default es no destruir.

## 5. Descubrimiento

Clasificación por contenido, con una sola apertura del zip por candidato (el original
lo abría tres veces: `is_zipfile`, clasificar, y otra vez para leerlo — en un XAPK de
2 GB el índice central está al final y cada apertura es un seek caro).

| Clase | Señal |
|---|---|
| `apk` | tiene una entrada `AndroidManifest.xml` |
| `container` | es zip y contiene `.apk` adentro, o entradas que la receta pide |
| `blob` | no es zip, o es un zip que la receta quiere copiar entero |

**Colapsamos `archive` y `loose` en `blob`.** En el original un OBB en formato zip —que
es la mayoría de los OBB de Unity y Unreal— quedaba clasificado como contenedor y una
regla que quisiera copiarlo entero **nunca lo encontraba**. Un mismo candidato puede ser
contenedor y blob a la vez; lo decide la regla, no el clasificador.

**Barrido:** se escanean `gamedata/` y la raíz del game_dir, y se **unen** los
resultados. Sin `prefer_first_nonempty` (un `.gitkeep` en `gamedata/` hacía que el APK
en la raíz nunca se mirara) y sin `sniff_all_in_primary` (dependía del índice en la
lista, no del primer directorio que existiera, así que fallaba justo cuando `gamedata/`
no estaba creado).

**Aislamiento por candidato:** un zip corrupto, con entradas duplicadas, con separadores
de Windows o con un `AndroidManifest.xml` decorativo se degrada a `skipped` con log. En
el original cualquiera de esos abortaba la instalación entera aunque el XAPK bueno
estuviera al lado.

**Manifest:** se parsea solo para sacar el `package`, que agrupa splits sueltos
(`base.apk` + `config.arm64_v8a.apk` se reconocen como un juego sin mirar los nombres) y
detecta bundles envenenados. Solo AXML binario — el soporte de XML plano del original es
superficie de ataque para un caso que no existe en APKs reales. El parser lleva límites
explícitos y **convierte todo error de parseo en un error de dominio**: en el original,
un pool de strings truncado en el byte justo escapaba como `IndexError` o `struct.error`
por todos los handlers y terminaba en un traceback de Python en pantalla.

**Expansión perezosa:** solo se materializa el grupo ganador. El original copiaba los
APKs internos de *todos* los XAPK descubiertos antes de decidir cuál usar — dos bundles
de 1.5 GB son 3 GB de copias en la SD, potencialmente para tirar.

### 5.1 Formas de entrada soportadas

| Forma | Cómo se maneja |
|---|---|
| APK único | zip; las reglas leen sus entradas |
| APK fat (varias ABI) | se elige la mejor ABI, no es ambigüedad (§6) |
| XAPK / APKM / APKS | se desempacan los APK internos a una cache y sus entradas quedan alcanzables |
| Splits sueltos | se agrupan por el `package` del AndroidManifest, no por nombre de archivo |
| OBB (zip o crudo) | contenedor y blob a la vez; lo decide la regla |
| **Carpeta ya descomprimida** | igual que un archivo; es la salida para fuentes en 7-Zip |

Las dos últimas filas son territorio virgen en el ecosistema: cero ports del catálogo
manejan XAPK o splits, y la ruta de la carpeta es lo que evita cargar un decodificador
7z para el caso de una fuente que sea un APK ya explotado dentro de ese formato.

El parser de AndroidManifest existe solo para leer el `package`. Devuelve `None` ante
cualquier duda: un paquete ilegible es una pista de agrupamiento que perdemos, nunca un
motivo para fallar. Está fuzzeado por truncado en cada offset y por flip de cada byte.

### 5.2 Raíces y composición de donors

Cada hijo inmediato de un directorio de búsqueda es un candidato. Una carpeta se
presenta como un árbol cuyas entradas son relativas a esa carpeta; no dispara un nuevo
descubrimiento recursivo de APKs o subcarpetas. `--input` repetido expresa el mismo
modelo de manera explícita y reemplaza el barrido automático para esa ejecución.

Dentro de un grupo, una regla `entries` puede reunir entradas de varios candidatos. Sus
validadores de árbol se aplican a lo reunido por **esa regla**, antes de fusionar los
resultados de reglas distintas. `required: false` solo permite cero coincidencias: si
hay alguna, debe satisfacer los validadores de la regla. La completitud de un árbol
construido por varias reglas se declara en el `validate` superior.

[`docs/DONORS.md`](docs/DONORS.md) es la referencia operativa de este contrato.

## 6. Selección de ABI

**El ABI no es una dimensión de ambigüedad, es una preferencia.** Esta es la corrección
más importante para el uso real.

El diseño original construía un plan por cada par `(grupo, ABI)` y, si dos planes
distintos validaban, fallaba con "multiple different payload sets match". Un APK fat
—que trae `lib/arm64-v8a/` **y** `lib/armeabi-v7a/`, o sea el caso normal— produce
exactamente eso: dos planes válidos, dos fingerprints, error. Su propia receta de
ejemplo falla contra un APK fat. Los tests no lo agarran porque solo cubren APKs de una
sola ABI.

Nuestro orden:

1. Dentro de cada grupo, elegir la mejor ABI según `abi_order`. Primer éxito gana.
2. Recién entonces comparar grupos entre sí por fingerprint.
3. Dos grupos con planes distintos → ahí sí, error de ambigüedad legítimo.

La validación de ABI es real, no textual: se lee `e_machine` del header ELF respetando
endianness. Agregamos el chequeo de `EI_CLASS` (32 vs 64 bits), que el original omitía —
sin eso una `.so` de ARMv5 valida como `armeabi-v7a`.

## 7. Receta

JSON, esquema versionado, compatible en espíritu con el formato del que partimos pero
recortado. El original tiene ~45 claves; el caso real de un port es *"sacá `lib/{abi}/*.so`
y `assets/**`, verificá el arch, instalá"*.

```json
{
  "schema": 1,
  "id": "port-id",
  "version": "1",
  "requires_eapx": ">=0.3.0",
  "title": "TÍTULO",
  "abi_order": ["arm64-v8a", "armeabi-v7a"],

  "extract": [
    {
      "id": "native-library",
      "description": "la librería nativa del juego",
      "destination": "lib/{abi}/libgame.so",
      "source": { "kind": "entry", "patterns": ["lib/{abi}/libgame.so"] },
      "validate": {
        "size": 4096,
        "sha256": "...",
        "critical_regions": {
          "regions": [{ "offset": "0x100", "size": 16 }],
          "sha256": "..."
        },
        "elf_machine": "{abi}"
      }
    },
    {
      "id": "game-assets",
      "destination": "assets",
      "source": { "kind": "entries", "patterns": ["assets/**"], "strip_prefix": "assets/" },
      "validate": { "min_files": 1, "min_bytes": 1 }
    }
  ],

  "hooks": [{ "id": "bake", "argv": ["{game_dir}/tools/bake"], "timeout_seconds": 600,
              "checkpoint": [{ "path": "assets/.ok", "sha256": "..." }] }],

  "profiles": [{ "id": "reference", "validate": [
    { "path": "assets/data/signature.bin", "sha256": "..." }
  ] }],

  "commit": ["lib/{abi}/libgame.so", "assets"],
  "marker": ".eapx-port-id.json",
  "space": { "safety_bytes": 134217728 }
}
```

### 7.1 Reglas del cargador

- **Whitelist estricta de claves en todos los niveles.** Un typo como `min_bites` o
  `sha_256` pasaba el `recipe-check` del original y desactivaba la validación en
  silencio. Para un formato cuya tesis entera es "validamos por contenido", ese es el
  agujero número uno en la práctica.
- **Booleanos son booleanos.** `"flatten": "no"` activaba flatten porque era truthy.
- **Validadores contradictorios se rechazan.** `{"size": 100, "min_size": 200}` garantiza
  que nada matchee nunca, y el error que veía el usuario apuntaba al APK.
- **Todo se valida al cargar**, no en runtime. En el original, `mode`, `env`, `cwd`,
  `scopes` y el formato de `tree_fingerprint` se validaban recién al usarlos: el
  `recipe-check` decía OK y la instalación reventaba a los tres minutos.
- **Claves de archivo en una regla `entries` son error**, no adorno. En el original,
  poner `sha256` o `elf_machine` en una regla de árbol no validaba absolutamente nada, y
  la documentación presentaba las dos listas sin advertirlo. Teatro de validación.
- `marker` y `log` no pueden vivir bajo una raíz de commit. Si lo hacen, se destruyen en
  cada reinstalación y el fast-path deja de funcionar para siempre, en silencio.

### 7.2 Regiones críticas

Una validación de archivo puede declarar `critical_regions` junto con un `size`
exacto. El tamaño se comprueba antes de leer contenido. Un `sha256` completo conocido
es el fast-path; si es desconocido, el motor concatena en orden los rangos declarados y
compara su SHA-256. Los offsets pueden ser enteros decimales o strings hexadecimales.

El motor no interpreta esos bytes ni conoce por qué son importantes. Elegir los rangos
que cubren offsets fijos, parches o datos estructurales es responsabilidad del port.
Recetas sin este campo conservan exactamente el comportamiento previo.

### 7.3 Digest

Dos digests separados:

- **Semántico** (`extract`, `commit`, `hooks`, `validate`, `profiles`, `abi_order`): invalida el
  marker y el stage.
- **Completo**: solo informativo.

En el original el digest cubría el título y los segundos de UI, así que corregir un typo
en un texto forzaba la reextracción completa de 2 GB.

### 7.4 Patterns

`fnmatch` sobre el nombre completo de la entrada, donde `*` cruza `/`. Se documenta
así explícitamente: el original lo llamaba "glob" en la doc, lo que hace pensar que
`**` es especial y que `*` no cruza directorios. Ninguna de las dos cosas es cierta.

## 8. Hooks

Lista argv, sin shell, sin excepciones. Templates por **sustitución literal** de las
cinco claves conocidas, no `str.format`: el original usaba format strings controladas
por la receta, lo que habilita navegar `__class__` / `__globals__` desde cualquier
valor. Contra una receta ya confiable no agrega poder, pero es un gadget gratis que no
hace falta regalar. Y cualquier error de template se convierte en error de dominio: un
typo como `{game_dir.foo}` levantaba `AttributeError`, escapaba todos los handlers, y la
UI se cerraba sin pintar el fallo — el usuario veía la pantalla desaparecer sin
explicación.

**Timeout obligatorio, con default de 600 s.** El reloj se reinicia con cada línea de
stdout, para no matar un hook lento pero vivo. Escalada `terminate` → `kill` sobre el
**grupo** de procesos, no sobre el pid: un hook que es un script de shell deja nietos.

Sin esto, un hook colgado deja el motor bloqueado en `readline()` para siempre, con el
lock tomado —así que ninguna corrida futura arranca— y la UI en pantalla completa con la
animación corriendo. El dispositivo queda colgado hasta que se reinicia. El código
original ya sabía hacer wait/terminate/kill escalonado; simplemente no lo aplicó acá.

## 9. Progreso y UI

**Archivo de estado, escritura atómica, sin fsync.** El modelo state-based es correcto
—cada escritura es el estado completo, no un delta, así que un lector que se pierde 40
actualizaciones lee la última— pero el fsync es puro daño: 25 fsync/s compitiendo con la
extracción en la misma SD, para un archivo cuya pérdida en un corte no importa nada
porque el estado real está en el journal.

Mensaje y detalle **acotados a 200 bytes**. Sin eso, un error largo (una ruta absoluta en
un `ValidationError`) desbordaba el buffer de 256 del lector, desalineaba el parseo de
las líneas siguientes, y terminaba dibujando la línea de protocolo cruda en pantalla
mientras la barra de fases retrocedía al segmento 0.

**Presupuesto de progreso por bytes reales, no por etapa.** Después del scan ya sabemos
cuánto pesa el bundle a expandir y cuánto el payload a copiar; con eso el porcentaje es
una estimación honesta. El reparto fijo del original daba 0% del presupuesto a la
expansión de bundles, que en un XAPK grande es la etapa más lenta: minutos de pantalla
clavada en 8% con la barra en cero.

**Sin fases mudas.** La expansión de bundles reporta por bloque. Y el resultado de la
validación de reanudación se cachea: el original hasheaba los mismos archivos hasta tres
veces —en el preflight de espacio, en el conteo de reanudación y en el loop de copia—
lo que en una reanudación de 2 GB son cuatro o cinco minutos de pantalla congelada
mostrando la fase anterior.

**La UI es estrictamente no-fatal** y **el motor nunca depende de ella.**

### 9.0 Tres canales, en orden de preferencia

Resuelto investigando el runtime real de PortMaster, no diseñando a ciegas.

1. **La barra nativa de PortMaster.** `pugwash` escucha en un FIFO
   (`/dev/shm/portmaster/pm_input`) y acepta `progress <texto> <hechos> <total> data`.
   Está implementado y **ningún port del catálogo lo usa**: los que quieren mostrar
   avance hardcodean el tamaño final y hacen polling con `du`. Nosotros sabemos
   exactamente cuántos bytes vamos a escribir, así que damos la barra real. El FIFO
   se abre en modo no bloqueante: si la GUI no está, da ENXIO en vez de colgar la
   instalación.
2. **stdout.** Bajo el patcher (LÖVE, 143 de 428 ports lo usan) cada línea se
   renderiza con efecto máquina de escribir. Emitimos una línea por cambio de fase,
   y las dos frases del protocolo: `Patching completed successfully!` y
   `Patching process failed!`. El detalle del error va como `PATCH_FAIL_MSG:` por el
   fd 3, para no ensuciar el log — sin eso el usuario recibe el genérico
   "go to the PortMaster Discord for help".
3. **La TTY del framebuffer.** Fallback cuando no hay nada de lo anterior. Los ports
   existentes ya escriben pantallas completas en `/dev/tty0`, así que la consola está
   libre: no hay que negociar con ningún servidor gráfico ni construir un binario SDL.

Los tres son no fatales y se degradan en cascada. La extracción nunca depende de que
haya alguien mirando.

### 9.1 Por qué no hay binario de UI

La duda original era si en una R36S stock EmulationStation suelta el DRM master, y
si no lo suelta, si toda una capa SDL sería código que nunca corre. La pregunta se
volvió irrelevante: los ports que ya funcionan escriben a `/dev/tty0` y la barra
nativa de PortMaster resuelve el caso bonito. Un binario en C de ~950 líneas para
dibujar una barra no se justifica cuando el runtime ya trae una.

## 10. Performance

Regla única: **cada byte se lee una vez.** Cache de digests por `(path, size, mtime_ns)`
compartido por todo el proceso, alimentando CRC32 y SHA-256 en el mismo recorrido.

Eso solo elimina cuatro duplicaciones distintas del original: la misma `.so` hasheada una
vez por cada par (grupo × ABI) al planificar; el CRC completo de cada candidato suelto
calculado siempre, aunque haya uno solo, solo para desempatar; el CRC completo de cada
APK cacheado en cada reanudación; y la validación completa con CRC de todos los árboles
una vez por ABI en la adopción.

También: contar los rechazos en el mismo loop que valida, en vez de la búsqueda lineal en
lista por cada elemento que hacía el original solo para armar el mensaje de error —con un
pattern que matchea 5000 entradas son 25 millones de comparaciones en una CPU de handheld.

## 11. Mensajes de error

El público es alguien escribiendo su primera receta a las 2 AM, leyendo en 640×480.

Cada regla que falla dice: qué buscó, cuántos candidatos matchearon, cuáles se
rechazaron, por qué validador exacto, y un ejemplo. El mejor mensaje del original ya
hacía esto; el resto decía cosas como "recipe selected no payload" sin indicar qué regla.

Dos herramientas de diagnóstico, que es lo que hoy falta:

- `eapx check <receta>` — sin necesitar el APK: claves desconocidas con sugerencia por
  distancia, validadores contradictorios, claves de archivo en reglas de árbol, destinos
  fuera de las raíces de commit expandidos por cada ABI, marker o log bajo una raíz de
  commit, y reglas sin ningún validador de contenido ("`game-assets` acepta cualquier
  cosa que matchee `assets/**`").
- `eapx explain <receta> <apk>` — por regla: qué matcheó, qué se rechazó, por qué. Es el
  bucle de feedback que hoy obliga a leer el log de una instalación fallida.

## 12. Fuera de alcance para la v1

APKM y APKS como formatos distintos (ya colapsan a "zip con APKs adentro"), `entry_or_file`
(innecesario si contenedor y blob se unifican), `scopes`, `case_sensitive`, `flatten`,
`crc32` como campo de receta (`sha256` lo subsume; CRC32 queda como optimización interna),
`magic_ascii`/`magic_hex`/`magic_offset` (`elf_machine` + `sha256` cubren el caso Android),
`required_paths` (se expresa mejor como reglas extra, con mejores errores), los límites de
bundle como configurables (constantes), `mode` (va en un hook), y `tree_fingerprint` en su
forma actual — sin un comando que lo calcule es inusable, y el mensaje de error ni siquiera
imprime el valor obtenido.

## 13. Tests que definen "terminado"

Los que el original no tiene y que cubren justamente lo que se le rompe:

1. Corte en cada paso del commit, con reejecución. El árbol termina sano o en el estado
   original, nunca mezclado.
2. **Corte en cada paso del rollback, con reejecución.** Reejecutar un rollback no puede
   destruir lo que ya restauró.
3. APK fat con dos ABIs → instala arm64, no falla por ambigüedad.
4. OBB en formato zip copiado entero por una regla de blob.
5. Archivo ajeno bajo una raíz de commit → falla con la lista, no lo borra.
6. Hook colgado → muere por timeout y libera el lock.
7. AXML truncado en cada offset posible → error de dominio, nunca traceback.
8. Receta con typo en una clave → error, no validación silenciosamente desactivada.
