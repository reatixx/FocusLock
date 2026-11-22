# FocusLock

**Un "bloqueo de estudio" para Windows con Claude como portero.**

Tengo un problema muy común: aplazo el estudio una y otra vez. Las apps de concentración típicas
se cierran con un clic, así que hice una herramienta que no me deja escaparme… salvo que tenga
una razón de verdad, y quien decide si la razón vale es una IA.

## Qué hace

- **Sesiones programadas.** El Programador de tareas de Windows lanza cada sesión a su hora
  (matemáticas, curso de IA, Java…). Se abre una ventana del navegador con las pestañas del curso
  a pantalla completa.
- **Bloqueo.** Mientras corre el temporizador, si cambio a otra ventana, FocusLock devuelve el
  curso al frente y cuenta el intento de "fuga". Las ventanas útiles se permiten: el IDE
  (IntelliJ IDEA en Java), ventanas emergentes del propio curso (SCORM), la barra de tareas.
  Los sitios de distracción (YouTube, Twitch, Reddit…) nunca cuentan como estudio.
- **Claude como portero.** Al lado hay un chat. Para salir antes o hacer una pausa hay que
  explicar por qué. Claude (vía `claude -p`, sin API key) responde y decide: `unlock`, `snooze`
  o seguir. "No me apetece" no es una razón; "me encuentro mal" sí.
- **Aplazar el inicio** de 10 a 60 minutos con un deslizador (una vez, antes de empezar),
  o mover una sesión solo por hoy: `focus.py postpone vass 20:30`.
- **Frase de emergencia** que libera al instante, sin IA.
- **Modo "Candidaturas"** conectado a mi otro proyecto, [JobHunter](https://github.com/reatixx/JobHunter):
  abre las ofertas preparadas (primero las de solicitud rápida en el propio portal),
  muestra la carta y el CV de cada una, estima un salario anual razonable en euros
  según el mercado del país, y tiene una ficha con mis datos para copiar en los formularios con un clic.
- Al terminar, guarda un resumen de la sesión en la memoria a largo plazo de Claude.

## Cómo funciona por dentro

- Python + `tkinter` para la ventana del temporizador y el chat.
- `ctypes` + Win32 API (`GetForegroundWindow`, `GetAncestor`, `SetForegroundWindow`,
  `EnumWindows`…) para detectar qué ventana está activa, a qué proceso pertenece y devolver el foco.
- `claude -p --output-format json` con herramientas desactivadas: el modelo solo devuelve
  `{"reply", "action", "minutes"}`, y el código aplica la acción.
- PowerShell (`install_tasks.ps1`) crea las tareas programadas a partir de `config.json`.

## Instalación

```powershell
copy config.example.json config.json     # tus sesiones, navegador y sitios bloqueados
copy profile.example.json profile.json   # opcional: datos para formularios
.\install_tasks.ps1                       # crea las tareas del Programador
python focus.py run math --minutes 1      # prueba rápida
```

Requisitos: Windows, Python 3.12+, [Claude Code](https://claude.com/claude-code) con sesión iniciada.
La interfaz y los mensajes de Claude están en ruso (es mi idioma nativo); se cambian en el código y en `chat_style`.

## Cómo lo hice

Proyecto hecho con **Claude Code** (desarrollo asistido por IA). Yo definí el problema y las reglas,
probé cada versión en mi día a día y fui encontrando los fallos: la ventana SCORM del curso que contaba
como fuga, las pausas que se sumaban dos veces y alargaban la sesión, el IDE bloqueado durante Java.
La IA escribió el código siguiendo esas indicaciones.
