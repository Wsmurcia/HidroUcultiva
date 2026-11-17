import network
import socket
import time
import json
from machine import Pin, ADC, PWM, I2C
import dht
import ssd1306

# ==========================================================
# 0. PARAMETROS DEL PROYECTO (modifica aqui cualquier valor)
# ==========================================================

# --- Riego (humedad del suelo + DHT22) ---
SUELO_UMBRAL_ACTIVACION = 30     # % : riego ON si el suelo esta por debajo
SUELO_UMBRAL_CALOR = 35          # % : umbral elevado cuando hace calor
TEMP_ALTA = 30                   # C : por encima de esto se usa el umbral de calor
SUELO_UMBRAL_CORTE = 70          # % : riego OFF si el suelo llega a este valor

# --- Reservorio (HC-SR04), distancias en cm medidas desde el sensor al agua ---
DIST_LLENADO_ON = 10             # >= 10 cm  -> nivel bajo, abrir llenado
DIST_LLENADO_OFF = 5             # <= 5 cm   -> tanque lleno, cerrar llenado
DIST_EMERGENCIA = 25             # > 25 cm   -> tanque casi vacio, BLOQUEA el riego
DIST_TANQUE_LLENO = 5.0          # calibracion para el % (0..100)
DIST_TANQUE_VACIO = 30.0

# --- LDR (Dia / Noche) ---
# Con el divisor de tension habitual, mas luz = lectura mas alta.
# Si en tu montaje es al reves, cambia LDR_MAS_LUZ_ES_MAYOR a False.
LDR_UMBRAL_DIA = 1500            # lectura ADC (0-4095)
LDR_MAS_LUZ_ES_MAYOR = True

# --- Humedad de suelo: calibracion del ADC ---
SUELO_ADC_SECO = 4095            # lectura con el sensor al aire (0 %)
SUELO_ADC_MOJADO = 1500          # lectura con el sensor en agua (100 %)

# --- Servomotores (posicionales, 0-180 grados) ---
SERVO1_PASO_GRADOS = 4           # grados por paso en el barrido del riego
SERVO_PASO_MS = 30               # ms entre pasos del barrido
SERVO2_ABIERTO = 90              # grados cuando el reservorio se llena (0 = cerrado)

# --- Tiempos del sistema ---
INTERVALO_SENSORES_MS = 2000     # el DHT22 necesita >= 2 s entre lecturas
INTERVALO_OLED_MS = 1000
TIMEOUT_SIMULACION_MS = 15000    # si la pagina deja de enviar, vuelve a modo real
FALLOS_US_MAX = 3                # lecturas ultrasonicas fallidas antes de modo seguro
MAX_EVENTOS = 40                 # eventos de servos guardados en la ESP32

# ==========================================================
# 1. WIFI
# ==========================================================

SSID_WIFI = "Redmi12"
PASSWORD_WIFI = "12345678"


def conectar_wifi():
    sta_if = network.WLAN(network.STA_IF)
    sta_if.active(True)

    if not sta_if.isconnected():
        print("Conectando al WiFi...")
        sta_if.connect(SSID_WIFI, PASSWORD_WIFI)
        inicio = time.time()
        while not sta_if.isconnected():
            if time.time() - inicio > 15:
                print("Iniciando AP de respaldo...")
                return iniciar_ap_respaldo()
            time.sleep(1)

    ip = sta_if.ifconfig()[0]
    print("Conexion exitosa. IP:", ip)
    return ip


def iniciar_ap_respaldo():
    ap_if = network.WLAN(network.AP_IF)
    ap_if.active(True)
    ap_if.config(essid="Hidrocultiva_ESP32", password="12345678")
    ip = ap_if.ifconfig()[0]
    print("Modo AP activo. IP:", ip)
    return ip


# ==========================================================
# 2. SENSORES
# ==========================================================

dht_sensor = dht.DHT22(Pin(4))

suelo_adc = ADC(Pin(34))
suelo_adc.atten(ADC.ATTN_11DB)

ldr_adc = ADC(Pin(35))
ldr_adc.atten(ADC.ATTN_11DB)

trig = Pin(5, Pin.OUT)
echo = Pin(18, Pin.IN)

# ==========================================================
# 3. SERVOMOTORES
# ==========================================================

servo_riego = PWM(Pin(25), freq=50)      # Servo 1: distribuidor de riego
servo_reservorio = PWM(Pin(26), freq=50)  # Servo 2: valvula del reservorio


def mover_servo(pwm_obj, angulo):
    """Angulo 0-180 -> pulso 0.5 ms - 2.5 ms a 50 Hz."""
    angulo = max(0, min(180, angulo))
    duty = int(26 + (angulo / 180.0) * 102)  # escala 0-1023
    try:
        pwm_obj.duty(duty)
    except AttributeError:
        pwm_obj.duty_u16(int(duty / 1023.0 * 65535))


servo1_angulo = 0
servo1_direccion = 1
servo2_angulo = 0

# ==========================================================
# 4. OLED
# ==========================================================

i2c = I2C(0, scl=Pin(22), sda=Pin(21))
try:
    oled = ssd1306.SSD1306_I2C(128, 64, i2c)
except Exception:
    oled = None

# ==========================================================
# 5. ESTADO GLOBAL
# ==========================================================

estado_suelo_val = 0.0
estado_temp_val = 0.0
estado_hum_aire_val = 0.0
estado_distancia_val = DIST_TANQUE_VACIO
estado_ldr_val = 0
es_dia = True

nivel_reserva_porc = 0
sensor_us_ok = True
fallos_us = 0

estado_riego_bool = False
estado_llenado_bool = False
bloqueo_emergencia = False
motivo_riego = "Esperando lecturas"
motivo_llenado = "Esperando lecturas"

modo_simulacion_web = False
ultimo_post_sim_ms = 0

# Registro de eventos de servomotores
BOOT_ID = time.ticks_ms() % 1000000
eventos = []
siguiente_evento_id = 1
riego_previo = False
llenado_previo = False
logica_inicializada = False


# ==========================================================
# 6. UTILIDADES
# ==========================================================

def calcular_nivel(distancia):
    nivel = (
        (DIST_TANQUE_VACIO - distancia)
        / (DIST_TANQUE_VACIO - DIST_TANQUE_LLENO)
    ) * 100
    return int(max(0, min(100, nivel)))


def nivel_a_distancia(nivel):
    """Inversa de calcular_nivel (para el modo simulacion)."""
    return DIST_TANQUE_VACIO - (nivel / 100.0) * (DIST_TANQUE_VACIO - DIST_TANQUE_LLENO)


def registrar_evento(servo, accion, motivo):
    global siguiente_evento_id
    eventos.append({
        "id": siguiente_evento_id,
        "servo": servo,          # "riego" (Servo 1) o "reservorio" (Servo 2)
        "accion": accion,        # "ACTIVADO" o "DETENIDO"
        "motivo": motivo,
        "suelo": estado_suelo_val,
        "temp": estado_temp_val,
        "reserva": nivel_reserva_porc,
        "t": time.ticks_ms()
    })
    siguiente_evento_id += 1
    if len(eventos) > MAX_EVENTOS:
        eventos.pop(0)
    print("EVENTO:", servo, accion, "-", motivo)


# ==========================================================
# 7. LECTURA DE SENSORES
# ==========================================================

def leer_ultrasonico():
    trig.value(0)
    time.sleep_us(2)
    trig.value(1)
    time.sleep_us(10)
    trig.value(0)

    inicio = time.ticks_us()
    while echo.value() == 0:
        if time.ticks_diff(time.ticks_us(), inicio) > 30000:
            return None

    t1 = time.ticks_us()
    while echo.value() == 1:
        if time.ticks_diff(time.ticks_us(), t1) > 30000:
            return None
    t2 = time.ticks_us()

    distancia = (time.ticks_diff(t2, t1) * 0.0343) / 2
    if distancia < 2 or distancia > 400:
        return None
    return distancia


def leer_sensores():
    global estado_suelo_val, estado_temp_val, estado_hum_aire_val
    global estado_distancia_val, nivel_reserva_porc
    global estado_ldr_val, es_dia
    global sensor_us_ok, fallos_us

    # DHT22
    try:
        dht_sensor.measure()
        estado_temp_val = dht_sensor.temperature()
        estado_hum_aire_val = dht_sensor.humidity()
    except Exception as e:
        print("Error DHT22:", e)

    # Humedad del suelo
    adc = suelo_adc.read()
    rango = SUELO_ADC_SECO - SUELO_ADC_MOJADO
    porc = (SUELO_ADC_SECO - adc) / rango * 100 if rango != 0 else 0
    estado_suelo_val = round(max(0, min(100, porc)), 1)

    # LDR
    estado_ldr_val = ldr_adc.read()
    if LDR_MAS_LUZ_ES_MAYOR:
        es_dia = estado_ldr_val > LDR_UMBRAL_DIA
    else:
        es_dia = estado_ldr_val < LDR_UMBRAL_DIA

    # Ultrasonico (promedio de 3 lecturas validas)
    lecturas = []
    for _ in range(3):
        d = leer_ultrasonico()
        if d is not None:
            lecturas.append(d)
        time.sleep_ms(30)

    if lecturas:
        estado_distancia_val = round(sum(lecturas) / len(lecturas), 1)
        nivel_reserva_porc = calcular_nivel(estado_distancia_val)
        fallos_us = 0
        sensor_us_ok = True
    else:
        fallos_us += 1
        if fallos_us >= FALLOS_US_MAX:
            sensor_us_ok = False
            print("Error: ultrasonico sin lectura valida")


# ==========================================================
# 8. LOGICA DEL SISTEMA
# ==========================================================

def actualizar_logica():
    global estado_riego_bool, estado_llenado_bool
    global bloqueo_emergencia, motivo_riego, motivo_llenado
    global riego_previo, llenado_previo, logica_inicializada

    dist = estado_distancia_val

    # --- Reservorio: histeresis por distancia ---
    if not sensor_us_ok:
        estado_llenado_bool = False
        motivo_llenado = "Sensor ultrasonico sin lectura (modo seguro)"
    elif dist >= DIST_LLENADO_ON:
        estado_llenado_bool = True
        motivo_llenado = "Nivel bajo: {:.1f} cm >= {} cm".format(dist, DIST_LLENADO_ON)
    elif dist <= DIST_LLENADO_OFF:
        estado_llenado_bool = False
        motivo_llenado = "Tanque lleno: {:.1f} cm <= {} cm".format(dist, DIST_LLENADO_OFF)
    # entre 5 y 10 cm se mantiene el estado anterior

    # --- Bloqueo de emergencia ---
    bloqueo_emergencia = (not sensor_us_ok) or (dist > DIST_EMERGENCIA)

    # --- Riego ---
    umbral = SUELO_UMBRAL_CALOR if estado_temp_val > TEMP_ALTA else SUELO_UMBRAL_ACTIVACION

    if bloqueo_emergencia:
        estado_riego_bool = False
        motivo_riego = "Bloqueo: reservorio vacio"
    elif not es_dia:
        estado_riego_bool = False
        motivo_riego = "Noche: riego bloqueado"
    elif estado_suelo_val >= SUELO_UMBRAL_CORTE:
        estado_riego_bool = False
        motivo_riego = "Suelo {:.0f}% >= corte {}%".format(estado_suelo_val, SUELO_UMBRAL_CORTE)
    elif estado_suelo_val < umbral:
        estado_riego_bool = True
        motivo_riego = "Suelo {:.0f}% < umbral {}%".format(estado_suelo_val, umbral)
    # entre umbral y corte se mantiene el estado anterior

    # --- Registro de eventos (solo cuando hay cambio) ---
    if logica_inicializada:
        if estado_riego_bool != riego_previo:
            registrar_evento(
                "riego",
                "ACTIVADO" if estado_riego_bool else "DETENIDO",
                motivo_riego
            )
        if estado_llenado_bool != llenado_previo:
            registrar_evento(
                "reservorio",
                "ACTIVADO" if estado_llenado_bool else "DETENIDO",
                motivo_llenado
            )
    logica_inicializada = True
    riego_previo = estado_riego_bool
    llenado_previo = estado_llenado_bool


# ==========================================================
# 9. SERVOS (se llama cada SERVO_PASO_MS)
# ==========================================================

def actualizar_servos():
    global servo1_angulo, servo1_direccion, servo2_angulo

    # Servo 1: barrido 0 <-> 180 mientras el riego esta activo
    if estado_riego_bool:
        servo1_angulo += servo1_direccion * SERVO1_PASO_GRADOS
        if servo1_angulo >= 180:
            servo1_angulo = 180
            servo1_direccion = -1
        elif servo1_angulo <= 0:
            servo1_angulo = 0
            servo1_direccion = 1
        mover_servo(servo_riego, servo1_angulo)
    elif servo1_angulo != 0:
        servo1_angulo = 0
        servo1_direccion = 1
        mover_servo(servo_riego, 0)

    # Servo 2: abierto mientras se llena el reservorio
    objetivo = SERVO2_ABIERTO if estado_llenado_bool else 0
    if objetivo != servo2_angulo:
        servo2_angulo = objetivo
        mover_servo(servo_reservorio, objetivo)


# ==========================================================
# 10. OLED
# ==========================================================

indice_pantalla = 0
tiempo_ultimo_cambio = time.time()


def actualizar_oled():
    global indice_pantalla, tiempo_ultimo_cambio
    if oled is None:
        return

    t_actual = time.time()
    if t_actual - tiempo_ultimo_cambio >= 3:
        indice_pantalla = (indice_pantalla + 1) % 4
        tiempo_ultimo_cambio = t_actual

    oled.fill(0)
    oled.text("-- HIDROCULTIVA --", 0, 0)

    if indice_pantalla == 0:
        oled.text("Suelo: {}%".format(estado_suelo_val), 0, 20)
        oled.text("Riego: {}".format("ACTIVO" if estado_riego_bool else "OFF"), 0, 35)
        oled.text("Luz: {}".format("Dia" if es_dia else "Noche"), 0, 50)
    elif indice_pantalla == 1:
        oled.text("Temp: {} C".format(estado_temp_val), 0, 20)
        oled.text("Hum Aire: {}%".format(estado_hum_aire_val), 0, 35)
    elif indice_pantalla == 2:
        oled.text("Tanque: {}%".format(nivel_reserva_porc), 0, 20)
        oled.text("Dist: {} cm".format(estado_distancia_val), 0, 35)
        oled.text("Llenado: {}".format("ON" if estado_llenado_bool else "OFF"), 0, 50)
    else:
        oled.text("Bloqueo: {}".format("SI" if bloqueo_emergencia else "NO"), 0, 20)
        oled.text("Modo: {}".format("SIMULACION" if modo_simulacion_web else "REAL"), 0, 35)

    oled.show()


# ==========================================================
# 11. SERVIDOR WEB (API REST)
# ==========================================================

CORS = (
    "Access-Control-Allow-Origin: *\r\n"
    "Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n"
    "Access-Control-Allow-Headers: Content-Type, Accept\r\n"
    "Access-Control-Allow-Private-Network: true\r\n"
)


def iniciar_servidor_web(ip):
    addr = socket.getaddrinfo(ip, 80)[0][-1]
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(addr)
    s.listen(3)
    s.setblocking(False)
    print("Servidor web escuchando en:", ip)
    return s


def construir_estado():
    ahora = time.ticks_ms()
    lista = []
    for ev in eventos[-20:]:
        lista.append({
            "id": ev["id"],
            "servo": ev["servo"],
            "accion": ev["accion"],
            "motivo": ev["motivo"],
            "suelo": ev["suelo"],
            "temp": ev["temp"],
            "reserva": ev["reserva"],
            "edad": time.ticks_diff(ahora, ev["t"]) // 1000   # segundos
        })

    return {
        "boot": BOOT_ID,
        "suelo": estado_suelo_val,
        "temp": estado_temp_val,
        "humAire": estado_hum_aire_val,
        "reserva": nivel_reserva_porc,
        "distancia": estado_distancia_val,
        "ldr": "Dia" if es_dia else "Noche",
        "dia": es_dia,
        "riego": estado_riego_bool,
        "bomba": estado_llenado_bool,
        "llenando": estado_llenado_bool,
        "bloqueo": bloqueo_emergencia,
        "motivoRiego": motivo_riego,
        "motivoLlenado": motivo_llenado,
        "servo1": servo1_angulo,
        "servo2": servo2_angulo,
        "simulacion": modo_simulacion_web,
        "eventos": lista
    }


def responder_json(cl, objeto, codigo="200 OK"):
    cuerpo = json.dumps(objeto)
    cabecera = (
        "HTTP/1.1 " + codigo + "\r\n"
        "Content-Type: application/json; charset=utf-8\r\n"
        + CORS +
        "Connection: close\r\n"
        "Content-Length: " + str(len(cuerpo.encode())) + "\r\n\r\n"
    )
    cl.send(cabecera.encode() + cuerpo.encode())


def leer_peticion(cl):
    """Lee cabecera + cuerpo completo (usa Content-Length)."""
    datos = b""
    while b"\r\n\r\n" not in datos:
        trozo = cl.recv(1024)
        if not trozo:
            break
        datos += trozo
        if len(datos) > 4096:
            break

    if b"\r\n\r\n" not in datos:
        return datos.decode("utf-8", "ignore"), ""

    cab, cuerpo = datos.split(b"\r\n\r\n", 1)
    cab_txt = cab.decode("utf-8", "ignore")

    largo = 0
    for linea in cab_txt.split("\r\n"):
        if linea.lower().startswith("content-length:"):
            largo = int(linea.split(":")[1].strip())

    while len(cuerpo) < largo:
        trozo = cl.recv(1024)
        if not trozo:
            break
        cuerpo += trozo

    return cab_txt, cuerpo.decode("utf-8", "ignore")


def manejar_peticiones(s):
    global estado_suelo_val, estado_temp_val, estado_hum_aire_val
    global nivel_reserva_porc, estado_distancia_val, es_dia
    global modo_simulacion_web, ultimo_post_sim_ms
    global sensor_us_ok, fallos_us

    try:
        cl, addr = s.accept()
    except OSError:
        return

    try:
        cl.settimeout(2)
        cabecera, cuerpo = leer_peticion(cl)
        primera = cabecera.split("\r\n")[0] if cabecera else ""

        if primera.startswith("OPTIONS"):
            cl.send(
                ("HTTP/1.1 204 No Content\r\n" + CORS +
                 "Connection: close\r\n\r\n").encode()
            )

        elif primera.startswith("POST /api/simulacion"):
            try:
                datos = json.loads(cuerpo)

                if datos.get("finSimulacion", False):
                    modo_simulacion_web = False
                    print("Simulacion finalizada")
                else:
                    if not modo_simulacion_web:
                        print("Simulacion iniciada desde la web")
                    modo_simulacion_web = True
                    ultimo_post_sim_ms = time.ticks_ms()

                    estado_suelo_val = float(datos.get("suelo", 68))
                    estado_temp_val = float(datos.get("temp", 17.5))
                    estado_hum_aire_val = float(datos.get("humAire", 72))
                    nivel = int(max(0, min(100, float(datos.get("reserva", 50)))))
                    nivel_reserva_porc = nivel
                    estado_distancia_val = round(nivel_a_distancia(nivel), 1)
                    es_dia = bool(datos.get("dia", True))
                    sensor_us_ok = True
                    fallos_us = 0

                    actualizar_logica()
                    actualizar_servos()

                responder_json(cl, construir_estado())

            except Exception as e:
                print("Error POST:", e)
                cl.send(
                    ("HTTP/1.1 400 Bad Request\r\n" + CORS +
                     "Connection: close\r\n\r\n").encode()
                )
        else:
            responder_json(cl, construir_estado())

    except Exception as e:
        print("Error peticion:", e)
    finally:
        try:
            cl.close()
        except Exception:
            pass


# ==========================================================
# 12. BUCLE PRINCIPAL
# ==========================================================

def main():
    global modo_simulacion_web

    mover_servo(servo_riego, 0)
    mover_servo(servo_reservorio, 0)

    ip_servidor = conectar_wifi()
    socket_servidor = iniciar_servidor_web(ip_servidor)

    print("--------------------------------")
    print("   SISTEMA HIDROCULTIVA")
    print("   IP para la pagina web:", ip_servidor)
    print("--------------------------------")

    leer_sensores()
    actualizar_logica()

    t_sensores = time.ticks_ms()
    t_servo = t_sensores
    t_oled = t_sensores

    while True:
        ahora = time.ticks_ms()

        # Salir de simulacion si la pagina dejo de enviar datos
        if modo_simulacion_web and time.ticks_diff(ahora, ultimo_post_sim_ms) > TIMEOUT_SIMULACION_MS:
            modo_simulacion_web = False
            print("Simulacion expirada: vuelvo a datos reales")

        if time.ticks_diff(ahora, t_sensores) >= INTERVALO_SENSORES_MS:
            t_sensores = ahora
            if not modo_simulacion_web:
                leer_sensores()
                actualizar_logica()

        if time.ticks_diff(ahora, t_servo) >= SERVO_PASO_MS:
            t_servo = ahora
            actualizar_servos()

        if time.ticks_diff(ahora, t_oled) >= INTERVALO_OLED_MS:
            t_oled = ahora
            actualizar_oled()

        manejar_peticiones(socket_servidor)
        time.sleep_ms(10)


if __name__ == "__main__":
    main()
