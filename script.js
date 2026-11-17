/**
 * Hidrocultiva - ESP32 Data Connector (script.js)
 * Comunicación vía API REST (fetch) con la ESP32:
 *   GET  /api/sensores     -> estado completo + eventos de servos
 *   POST /api/simulacion   -> envía valores simulados (responde con el estado)
 */

const ESP32_CONFIG = {
    ip: '10.77.84.74', // IP que imprime la consola de la ESP32 (se puede cambiar desde la página)
    intervaloLecturaMs: 3000,
    intentosFallidosMax: 3,
    timeoutMs: 2500
};

// La IP se recuerda entre sesiones
try {
    const ipGuardada = localStorage.getItem('hidro_esp32_ip');
    if (ipGuardada) ESP32_CONFIG.ip = ipGuardada;
} catch (e) { /* localStorage no disponible */ }

let esp32Timer = null;
let fallosConexionCount = 0;
let esp32Conectada = false;

function fetchConTimeout(url, opciones = {}) {
    const controller = new AbortController();
    const id = setTimeout(() => controller.abort(), ESP32_CONFIG.timeoutMs);
    return fetch(url, { ...opciones, signal: controller.signal })
        .finally(() => clearTimeout(id));
}

function pintarEstadoConexion(conectada, texto) {
    const badge = document.getElementById('badge-conexion-esp32');
    const txt = document.getElementById('texto-estado-conexion');
    const punto = document.getElementById('punto-esp32');
    if (!badge) return;
    badge.className = conectada ? 'estado optimo' : 'estado inactivo';
    txt.textContent = texto;
    punto.style.backgroundColor = conectada ? '#2ecc71' : '#e74c3c';
}

function iniciarConexionESP32() {
    console.log('Conectando con la ESP32 en http://' + ESP32_CONFIG.ip);
    if (esp32Timer) clearInterval(esp32Timer);
    obtenerTelemetriaESP32();
    esp32Timer = setInterval(obtenerTelemetriaESP32, ESP32_CONFIG.intervaloLecturaMs);
}

async function obtenerTelemetriaESP32() {
    // Mientras se simula el lote físico, los datos llegan en la respuesta del POST
    if (typeof simulandoLoteFisico === 'function' && simulandoLoteFisico()) return;

    try {
        const respuesta = await fetchConTimeout(`http://${ESP32_CONFIG.ip}/api/sensores`, {
            method: 'GET',
            headers: { 'Accept': 'application/json' }
        });
        if (!respuesta.ok) throw new Error(`HTTP ${respuesta.status}`);

        const datos = await respuesta.json();
        fallosConexionCount = 0;
        esp32Conectada = true;
        pintarEstadoConexion(true, datos.simulacion ? 'ESP32 Conectada (en simulación)' : 'ESP32 Conectada');

        if (typeof recibirTelemetriaESP32 === 'function') recibirTelemetriaESP32(datos);
    } catch (error) {
        fallosConexionCount++;
        console.warn(`Intento ${fallosConexionCount}: sin respuesta de la ESP32 (${error.message})`);
        if (fallosConexionCount >= ESP32_CONFIG.intentosFallidosMax) marcarESP32Desconectada();
    }
}

/**
 * Envía valores simulados a la ESP32. La ESP32 ejecuta su lógica real
 * (servos incluidos) con esos valores y responde con el estado resultante.
 * Devuelve el estado, o null si no hubo respuesta.
 */
async function enviarSimulacionESP32(payload) {
    try {
        const respuesta = await fetchConTimeout(`http://${ESP32_CONFIG.ip}/api/simulacion`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        if (!respuesta.ok) throw new Error(`HTTP ${respuesta.status}`);

        const datos = await respuesta.json();
        fallosConexionCount = 0;
        esp32Conectada = true;
        pintarEstadoConexion(true, payload.finSimulacion ? 'ESP32 Conectada' : 'ESP32 Conectada (simulando)');
        return datos;
    } catch (error) {
        console.error('Error al enviar la simulación a la ESP32:', error.message);
        fallosConexionCount++;
        if (fallosConexionCount >= ESP32_CONFIG.intentosFallidosMax) marcarESP32Desconectada();
        return null;
    }
}

function marcarESP32Desconectada() {
    esp32Conectada = false;
    pintarEstadoConexion(false, 'ESP32 Desconectada');
}

function cambiarIpESP32() {
    const nueva = prompt('IP de la ESP32 (la muestra la consola al arrancar):', ESP32_CONFIG.ip);
    if (!nueva || !nueva.trim()) return;
    ESP32_CONFIG.ip = nueva.trim();
    try { localStorage.setItem('hidro_esp32_ip', ESP32_CONFIG.ip); } catch (e) { /* ignorar */ }
    fallosConexionCount = 0;
    iniciarConexionESP32();
}

window.ESP32Connector = {
    config: ESP32_CONFIG,
    iniciar: iniciarConexionESP32,
    obtenerTelemetria: obtenerTelemetriaESP32,
    enviarSimulacion: enviarSimulacionESP32,
    estaConectada: () => esp32Conectada,
    cambiarIp: cambiarIpESP32
};
