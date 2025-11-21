import streamlit as st
import pandas as pd
import sqlite3
from datetime import datetime
import time
import io
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import pytz
import streamlit.components.v1 as components

# --- 1. CONFIGURACIÓN DEL NEGOCIO ---
NOMBRE_NEGOCIO = "Papelería La Esperanza"
UBICACION = "Guadalajara, Jal."
MONEDA = "$"
TIMEOUT_SEGUNDOS = 3600 # 1 Hora de inactividad

st.set_page_config(page_title=NOMBRE_NEGOCIO, layout="wide", page_icon="📒")

# --- ESTILOS PROFESIONALES (WHITELABEL) ---
st.markdown("""
    <style>
    /* 1. Ocultar la barra superior completa (Header) */
    [data-testid="stHeader"] {
        display: none;
    }
    
    /* 2. Ocultar el menú de opciones (Hamburguesa) */
    [data-testid="stToolbar"] {
        visibility: hidden;
    }

    /* 3. Ocultar el pie de página (Made with Streamlit) */
    footer {
        visibility: hidden;
    }
    
    /* 4. Ajustar el espacio para que el contenido suba y aproveche la pantalla */
    .block-container {
        padding-top: 1rem;
        padding-bottom: 1rem;
    }

    /* Estilos del Ticket y Totales */
    .ticket { 
        background-color: #fff; 
        color: #000; 
        padding: 20px; 
        border: 1px dashed #333; 
        font-family: 'Courier New', Courier, monospace; 
        font-size: 12px; 
        line-height: 1.2;
        box-shadow: 0 4px 6px rgba(0,0,0,0.1);
    }
    .big-total { 
        font-size: 28px; 
        font-weight: bold; 
        color: #2E7D32; 
    }
    </style>
    """, unsafe_allow_html=True)

# --- ESTADO DE SESIÓN ---
if 'logged_in' not in st.session_state: st.session_state.logged_in = False
if 'usuario_actual' not in st.session_state: st.session_state.usuario_actual = None
if 'rol_actual' not in st.session_state: st.session_state.rol_actual = None
if 'carrito' not in st.session_state: st.session_state.carrito = []
if 'inventario_sincronizado' not in st.session_state: st.session_state.inventario_sincronizado = False
if 'editando_id' not in st.session_state: st.session_state.editando_id = None
if 'ultima_sinc' not in st.session_state: st.session_state.ultima_sinc = "Pendiente"
if 'last_active' not in st.session_state: st.session_state.last_active = time.time()

# --- JAVASCRIPT: AUTO-FOCUS ---
def set_focus_on_scan():
    """Inyecta JS para poner el cursor en el input de escaneo automáticamente"""
    components.html(
        f"""
            <script>
                var input = window.parent.document.querySelectorAll("input[type=text]");
                for (var i = 0; i < input.length; ++i) {{
                    if (input[i].ariaLabel == "Escanear (Enter)") {{
                        input[i].focus();
                    }}
                }}
            </script>
        """,
        height=0
    )

# --- SEGURIDAD: TIMEOUT ---
def check_timeout():
    if st.session_state.logged_in:
        now = time.time()
        if (now - st.session_state.last_active) > TIMEOUT_SEGUNDOS:
            logout()
        else:
            st.session_state.last_active = now

# --- HORA LOCAL ---
def hora_actual():
    zona_mx = pytz.timezone('America/Mexico_City')
    return datetime.now(zona_mx).strftime("%Y-%m-%d %H:%M:%S")

# --- BASE DE DATOS LOCAL (SQLite) ---
@st.cache_resource
def get_sql_connection():
    return sqlite3.connect('inventario.db', check_same_thread=False)

def init_local_db():
    conn = get_sql_connection()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS productos (id INTEGER PRIMARY KEY AUTOINCREMENT, codigo_barra TEXT UNIQUE, nombre TEXT, precio REAL, stock INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS usuarios (id INTEGER PRIMARY KEY AUTOINCREMENT, nombre TEXT UNIQUE, password TEXT, rol TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS ventas (id INTEGER PRIMARY KEY AUTOINCREMENT, fecha TIMESTAMP, total REAL, vendedor TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS detalle_ventas (id INTEGER PRIMARY KEY AUTOINCREMENT, venta_id INTEGER, producto_nombre TEXT, cantidad INTEGER, precio_unitario REAL, subtotal REAL, FOREIGN KEY(venta_id) REFERENCES ventas(id))''')
    
    c.execute('SELECT count(*) FROM usuarios')
    if c.fetchone()[0] == 0:
        if "general" in st.secrets and "admin_password" in st.secrets["general"]:
            pass_admin = st.secrets["general"]["admin_password"]
        else:
            pass_admin = "admin123" 
        c.execute("INSERT INTO usuarios (nombre, password, rol) VALUES ('Admin', ?, 'Gerente')", (pass_admin,))
        c.execute("INSERT INTO usuarios (nombre, password, rol) VALUES ('Cajero1', '1234', 'Empleado')")
    conn.commit()

init_local_db()
conn = get_sql_connection()

# --- CONEXIÓN GOOGLE SHEETS ---
def get_gsheet_client():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    try:
        return gspread.authorize(ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope))
    except FileNotFoundError:
        return gspread.authorize(ServiceAccountCredentials.from_json_keyfile_dict(st.secrets["gcp_service_account"], scope))

# --- FUNCIONES DE SINCRONIZACIÓN ---
def sincronizar_inventario_descarga():
    try:
        client = get_gsheet_client()
        sheet = client.open("PapeleriaDB").worksheet("Productos")
        datos = sheet.get_all_records()
        c = conn.cursor()
        c.execute("DELETE FROM productos") 
        if datos:
            for p in datos:
                if str(p['Codigo']) != "":
                    c.execute("INSERT INTO productos (codigo_barra, nombre, precio, stock) VALUES (?, ?, ?, ?)",
                              (str(p['Codigo']), p['Nombre'], float(p['Precio']), int(p['Stock'])))
            conn.commit()
            st.session_state.ultima_sinc = hora_actual()
            return True, f"Sincronizado: {len(datos)} productos."
        return True, "Nube vacía."
    except Exception as e: return False, f"Error: {e}"

def guardar_producto_nube(codigo, nombre, precio, stock):
    try:
        client = get_gsheet_client()
        sheet = client.open("PapeleriaDB").worksheet("Productos")
        sheet.append_row([str(codigo), nombre, precio, stock])
        return True
    except: return False

def editar_producto_nube(codigo_original, nuevo_nombre, nuevo_precio, nuevo_stock):
    try:
        client = get_gsheet_client()
        sheet = client.open("PapeleriaDB").worksheet("Productos")
        cell = sheet.find(str(codigo_original))
        if cell:
            sheet.update_cell(cell.row, 2, nuevo_nombre)
            sheet.update_cell(cell.row, 3, nuevo_precio)
            sheet.update_cell(cell.row, 4, nuevo_stock)
            return True
        return False
    except: return False

def eliminar_producto_nube(codigo):
    try:
        client = get_gsheet_client()
        sheet = client.open("PapeleriaDB").worksheet("Productos")
        cell = sheet.find(str(codigo))
        if cell: sheet.delete_rows(cell.row); return True
    except: return False

def actualizar_stock_nube_lote(lista_cambios):
    try:
        client = get_gsheet_client()
        sheet = client.open("PapeleriaDB").worksheet("Productos")
        todos = sheet.get_all_records()
        batch = []
        mapa = {str(p['Codigo']): i + 2 for i, p in enumerate(todos)}
        
        for cod, cant in lista_cambios:
            scod = str(cod)
            if scod in mapa:
                fila = mapa[scod]
                curr = next((p['Stock'] for p in todos if str(p['Codigo']) == scod), 0)
                batch.append({'range': f'D{fila}', 'values': [[int(curr) - cant]]})
        
        if batch: 
            sheet.batch_update(batch)
            st.session_state.ultima_sinc = hora_actual()
            return True
        return False
    except: return False

def registrar_venta_nube_historial(fecha, ticket_id, vendedor, total, resumen):
    try:
        client = get_gsheet_client()
        sheet = client.open("PapeleriaDB").worksheet("Ventas")
        sheet.append_row([str(fecha), ticket_id, vendedor, total, resumen])
        return True
    except: return False

# --- LÓGICA PRINCIPAL ---
def login(u, p):
    df = pd.read_sql("SELECT * FROM usuarios WHERE nombre=? AND password=?", conn, params=(u,p))
    if not df.empty:
        st.session_state.logged_in=True; st.session_state.usuario_actual=df.iloc[0]['nombre']; st.session_state.rol_actual=df.iloc[0]['rol']
        st.session_state.last_active = time.time()
        st.rerun()
    else: st.error("Datos incorrectos")

def logout():
    for key in list(st.session_state.keys()):
        del st.session_state[key]
    st.rerun()

def scan_callback():
    st.session_state.last_active = time.time() 
    codigo = st.session_state.input_scan
    if codigo:
        df = pd.read_sql("SELECT * FROM productos WHERE codigo_barra = ?", conn, params=(codigo,))
        if df.empty: df = pd.read_sql("SELECT * FROM productos WHERE nombre LIKE ?", conn, params=(f"%{codigo}%",))
        if not df.empty:
            prod = df.iloc[0]
            cant = st.session_state.qty_scan
            if cant <= prod['stock']:
                 found = False
                 for item in st.session_state.carrito:
                     if item['id'] == prod['id']:
                         item['cantidad'] += cant
                         item['subtotal'] = item['cantidad'] * item['precio']
                         found = True; break
                 if not found:
                     st.session_state.carrito.append({"id": prod['id'], "codigo": prod['codigo_barra'], "nombre": prod['nombre'], "precio": prod['precio'], "cantidad": cant, "subtotal": cant*prod['precio']})
                 st.toast(f"✅ {prod['nombre']}")
            else: st.error("Stock insuficiente")
        else: st.toast("❌ No encontrado")
    st.session_state.input_scan = ""

def procesar_venta_final(vendedor, pago):
    st.session_state.last_active = time.time()
    total = sum(i['subtotal'] for i in st.session_state.carrito)
    fecha = hora_actual()
    
    c = conn.cursor()
    c.execute("INSERT INTO ventas (fecha, total, vendedor) VALUES (?,?,?)", (fecha, total, vendedor))
    v_id = c.lastrowid
    
    resumen = ""
    ticket = f"{NOMBRE_NEGOCIO}\n{UBICACION}\n\nTICKET #{v_id}\nFECHA: {fecha}\nATENDIÓ: {vendedor}\n{'-'*30}\n"
    
    cambios_nube = []
    for item in st.session_state.carrito:
        c.execute("INSERT INTO detalle_ventas (venta_id, producto_nombre, cantidad, precio_unitario, subtotal) VALUES (?,?,?,?,?)", 
                  (v_id, item['nombre'], item['cantidad'], item['precio'], item['subtotal']))
        c.execute("UPDATE productos SET stock = stock - ? WHERE codigo_barra = ?", (item['cantidad'], item['codigo']))
        cambios_nube.append((item['codigo'], item['cantidad']))
        ticket += f"{item['cantidad']} x {item['nombre'][:15]:<15} ${item['subtotal']:>6.2f}\n"
        resumen += f"({item['cantidad']}){item['nombre']}, "

    ticket += f"{'-'*30}\nTOTAL : {MONEDA}{total:>8.2f}\nPAGO  : {MONEDA}{pago:>8.2f}\nCAMBIO: {MONEDA}{pago-total:>8.2f}\n{'-'*30}\n¡Gracias por su compra!"
    conn.commit()
    
    with st.spinner("Guardando..."):
        registrar_venta_nube_historial(fecha, v_id, vendedor, total, resumen)
        actualizar_stock_nube_lote(cambios_nube)
    
    st.session_state.carrito = []
    return ticket

def to_excel(df):
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer: df.to_excel(writer, index=False)
    return output.getvalue()

# --- EJECUCIÓN ---
check_timeout() 

if not st.session_state.inventario_sincronizado:
    with st.spinner("🔄 Iniciando sistema..."):
        sincronizar_inventario_descarga()
    st.session_state.inventario_sincronizado = True

# --- FRONTEND ---
if not st.session_state.logged_in:
    col1, col2, col3 = st.columns([1,2,1])
    with col2:
        st.markdown(f"<h2 style='text-align: center;'>🔒 Acceso {NOMBRE_NEGOCIO}</h2>", unsafe_allow_html=True)
        st.write("")
        with st.form("log"):
            u = st.text_input("Usuario"); p = st.text_input("Password", type="password")
            if st.form_submit_button("Iniciar Sesión", type="primary"): login(u,p)
else:
    with st.sidebar:
        st.markdown(f"### {NOMBRE_NEGOCIO}")
        st.markdown("🟢 **Sistema En Línea**")
        st.caption(f"Sincronizado: {st.session_state.ultima_sinc}")
        st.divider()
        st.write(f"👤 **{st.session_state.usuario_actual}**")
        if st.button("Cerrar Sesión"): logout()
        st.divider()
        if st.button("☁️ Forzar Recarga"):
            sincronizar_inventario_descarga(); st.rerun()

    menu = st.sidebar.radio("Ir a:", ["Punto de Venta", "Reportes", "Inventario", "Usuarios"])

    if menu == "Punto de Venta":
        st.subheader("🛒 Caja Registradora")
        set_focus_on_scan()
        
        c_scan, c_qty = st.columns([3, 1])
        with c_qty: st.number_input("Cant", 1, 100, 1, key="qty_scan")
        with c_scan: st.text_input("Escanear (Enter)", key="input_scan", on_change=scan_callback)

        if st.session_state.carrito:
            for i, item in enumerate(st.session_state.carrito):
                c1, c2, c3, c4, c5 = st.columns([3, 1, 1, 1, 0.5])
                c1.write(f"**{item['nombre']}**")
                c2.write(f"${item['precio']}")
                c3.write(f"x{item['cantidad']}")
                c4.write(f"${item['subtotal']:.2f}")
                if c5.button("❌", key=f"d_c_{i}"): st.session_state.carrito.pop(i); st.rerun()
            st.divider()
            total = sum(i['subtotal'] for i in st.session_state.carrito)
            c_tot, c_pag = st.columns(2)
            with c_tot: st.markdown(f"<div class='big-total'>Total: ${total:,.2f}</div>", unsafe_allow_html=True)
            with c_pag: pago = st.number_input("💵 Pago Cliente:", min_value=0.0, value=float(total))
            if st.button("✅ COBRAR", type="primary", use_container_width=True):
                if pago >= total:
                    ticket = procesar_venta_final(st.session_state.usuario_actual, pago)
                    st.balloons()
                    c1, c2 = st.columns([1,2])
                    with c1: st.markdown(f'<div class="ticket"><pre>{ticket}</pre></div>', unsafe_allow_html=True)
                    with c2: st.success("Venta Exitosa")
                    time.sleep(2); st.rerun()
                else: st.error("Faltan fondos")

    elif menu == "Reportes":
        st.subheader("📊 Dashboard Financiero")
        df_ventas = pd.read_sql("SELECT * FROM ventas", conn)
        df_detalles = pd.read_sql("SELECT * FROM detalle_ventas", conn)
        if not df_ventas.empty:
            k1, k2, k3 = st.columns(3)
            total_ing = df_ventas['total'].sum()
            k1.metric("💰 Ingresos", f"${total_ing:,.2f}")
            k2.metric("🧾 Tickets", len(df_ventas))
            k3.metric("📈 Promedio", f"${total_ing/len(df_ventas):,.2f}")
            st.divider()
            c1, c2 = st.columns(2)
            with c1:
                st.markdown("##### 🏆 Top Productos")
                if not df_detalles.empty: st.bar_chart(df_detalles.groupby('producto_nombre')['cantidad'].sum().sort_values(ascending=False).head(5))
            with c2:
                st.markdown("##### 📅 Ventas por Hora")
                df_ventas['fecha'] = pd.to_datetime(df_ventas['fecha'])
                st.line_chart(df_ventas.groupby(df_ventas['fecha'].dt.hour)['total'].sum())
            st.download_button("📥 Excel", to_excel(df_ventas), "reporte.xlsx")
        else: st.warning("Sin ventas.")

    elif menu == "Inventario":
        st.subheader("📦 Inventario Nube")
        if st.session_state.editando_id:
            p_edit = pd.read_sql(f"SELECT * FROM productos WHERE id={st.session_state.editando_id}", conn).iloc[0]
            with st.form("edit"):
                nn=st.text_input("Nombre", value=p_edit['nombre']); np=st.number_input("Precio", value=float(p_edit['precio'])); ns=st.number_input("Stock", value=int(p_edit['stock']))
                if st.form_submit_button("Guardar"):
                    if editar_producto_nube(p_edit['codigo_barra'], nn, np, ns):
                        sincronizar_inventario_descarga(); st.session_state.editando_id=None; st.rerun()
            if st.button("Cancelar"): st.session_state.editando_id=None; st.rerun()
        else:
            with st.expander("➕ Nuevo Producto"):
                c1,c2,c3,c4=st.columns(4)
                nc=c1.text_input("Código", key="n_c"); nn=c2.text_input("Nombre", key="n_n"); np=c3.number_input("Precio",0.0, key="n_p"); ns=c4.number_input("Stock",1, key="n_s")
                if st.button("Guardar"):
                    if nc and nn: guardar_producto_nube(nc,nn,np,ns); sincronizar_inventario_descarga(); st.success("OK"); st.rerun()
            df = pd.read_sql("SELECT * FROM productos", conn)
            st.dataframe(df[['codigo_barra', 'nombre', 'precio', 'stock']], use_container_width=True)
            st.divider()
            col_act1, col_act2 = st.columns(2)
            with col_act1: prod_accion = st.selectbox("Selecciona producto:", df['nombre'])
            with col_act2:
                st.write(""); st.write("")
                c_a, c_b = st.columns(2)
                if c_a.button("✏️ Editar"): st.session_state.editando_id = df[df['nombre'] == prod_accion].iloc[0]['id']; st.rerun()
                if c_b.button("🗑️ Borrar"): eliminar_producto_nube(df[df['nombre'] == prod_accion].iloc[0]['codigo_barra']); sincronizar_inventario_descarga(); st.rerun()

    elif menu == "Usuarios":
        st.subheader("👥 Personal")
        with st.form("nu"):
            un=st.text_input("Usuario"); up=st.text_input("Pass", type="password"); ur=st.selectbox("Rol",["Empleado","Gerente"])
            if st.form_submit_button("Crear"):
                try: c=conn.cursor(); c.execute("INSERT INTO usuarios (nombre,password,rol) VALUES (?,?,?)",(un,up,ur)); conn.commit(); st.success("OK")
                except: st.error("Error")
        st.dataframe(pd.read_sql("SELECT nombre, rol FROM usuarios", conn))