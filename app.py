import json
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode

import requests
import streamlit as st
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import service_account


# =========================================================
# CONFIGURAÇÃO
# =========================================================

st.set_page_config(
    page_title="Route Optimizer",
    page_icon="🚚",
    layout="wide",
)

st.title("🚚 Route Optimizer")
st.caption("Planeamento, validação e navegação de rotas")


# =========================================================
# SESSION STATE
# =========================================================

if "route_data" not in st.session_state:
    st.session_state.route_data = None

if "validated" not in st.session_state:
    st.session_state.validated = False

if "manual_order" not in st.session_state:
    st.session_state.manual_order = None


# =========================================================
# AUTENTICAÇÃO GOOGLE
# =========================================================

@st.cache_resource
def get_credentials():
    info = json.loads(
        st.secrets["GCP_SERVICE_ACCOUNT_JSON"]
    )

    return service_account.Credentials.from_service_account_info(
        info,
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )


def get_access_token():
    credentials = get_credentials()

    if not credentials.valid:
        credentials.refresh(GoogleAuthRequest())

    return credentials.token


def auth_headers():
    return {
        "Authorization": f"Bearer {get_access_token()}",
        "Content-Type": "application/json",
    }


# =========================================================
# GEOCODING
# =========================================================

@st.cache_data(show_spinner=False)
def geocode_address(address):
    address = address.strip()

    encoded_address = quote(address, safe="")

    url = (
        "https://geocode.googleapis.com/v4/geocode/address/"
        f"{encoded_address}"
        "?regionCode=pt&languageCode=pt"
    )

    headers = auth_headers()

    headers["X-Goog-FieldMask"] = (
        "results.location,"
        "results.formattedAddress,"
        "results.placeId"
    )

    response = requests.get(
        url,
        headers=headers,
        timeout=30,
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"Erro ao localizar '{address}': "
            f"{response.status_code} - {response.text}"
        )

    results = response.json().get("results", [])

    if not results:
        raise ValueError(
            f"Não foi possível localizar: {address}"
        )

    result = results[0]

    return {
        "original": address,
        "formatted": result.get(
            "formattedAddress",
            address,
        ),
        "place_id": result.get("placeId"),
        "latitude": result["location"]["latitude"],
        "longitude": result["location"]["longitude"],
    }


# =========================================================
# HELPERS
# =========================================================

def waypoint(location):
    return {
        "location": {
            "latLng": {
                "latitude": location["latitude"],
                "longitude": location["longitude"],
            }
        }
    }


def duration_seconds(value):
    if not value:
        return 0

    if isinstance(value, str) and value.endswith("s"):
        try:
            return float(value[:-1])
        except ValueError:
            return 0

    return 0


def format_duration(seconds):
    seconds = int(round(seconds))

    hours = seconds // 3600
    minutes = (seconds % 3600) // 60

    if hours:
        return f"{hours} h {minutes:02d} min"

    return f"{minutes} min"


# =========================================================
# ROUTE OPTIMIZATION API
# =========================================================

def optimize_route(
    origin,
    clients,
    round_trip=False,
    avoid_tolls=True,
):
    info = json.loads(
        st.secrets["GCP_SERVICE_ACCOUNT_JSON"]
    )

    project_id = info["project_id"]

    url = (
        "https://routeoptimization.googleapis.com/v1/"
        f"projects/{project_id}:optimizeTours"
    )

    now = datetime.now(timezone.utc)
    start_time = now.replace(microsecond=0)
    end_time = start_time + timedelta(hours=24)

    shipments = []

    for client in clients:
        shipments.append(
            {
                "label": client["original"],
                "deliveries": [
                    {
                        "arrivalWaypoint": waypoint(client),
                        "duration": "0s",
                        "label": client["original"],
                    }
                ],
                "penaltyCost": 1000000,
            }
        )

    vehicle = {
        "label": "Viatura 1",
        "startWaypoint": waypoint(origin),
        "travelMode": "DRIVING",
        "routeModifiers": {
            "avoidTolls": avoid_tolls,
            "avoidHighways": False,
            "avoidFerries": True,
        },
        "costPerTraveledHour": 1.0,
    }

    if round_trip:
        vehicle["endWaypoint"] = waypoint(origin)

    body = {
        "timeout": "30s",
        "considerRoadTraffic": True,
        "model": {
            "shipments": shipments,
            "vehicles": [vehicle],
            "globalStartTime": (
                start_time.isoformat().replace("+00:00", "Z")
            ),
            "globalEndTime": (
                end_time.isoformat().replace("+00:00", "Z")
            ),
        },
    }

    response = requests.post(
        url,
        headers=auth_headers(),
        json=body,
        timeout=45,
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"Erro Route Optimization API:\n\n"
            f"{response.status_code}\n"
            f"{response.text}"
        )

    return response.json()


# =========================================================
# GOOGLE MAPS
# =========================================================

def google_maps_url(
    origin,
    waypoints,
    destination,
    avoid_tolls=True,
    navigation=False,
):
    params = {
        "api": "1",
        "origin": origin,
        "destination": destination,
        "travelmode": "driving",
    }

    if avoid_tolls:
        params["avoid"] = "tolls"

    if waypoints:
        params["waypoints"] = "|".join(waypoints)

    if navigation:
        params["dir_action"] = "navigate"

    return (
        "https://www.google.com/maps/dir/?"
        + urlencode(params, safe="|,")
    )


# =========================================================
# PRÉ-VISUALIZAÇÃO
# =========================================================

def build_preview_links(
    origin,
    ordered_clients,
    round_trip,
    avoid_tolls,
):
    if not ordered_clients:
        return []

    points = [origin["formatted"]]

    points.extend(
        client["formatted"]
        for client in ordered_clients
    )

    if round_trip:
        points.append(origin["formatted"])

    links = []

    # Preview pode usar mais pontos
    max_points = 11

    start = 0
    number = 1

    while start < len(points) - 1:
        segment = points[start:start + max_points]

        if len(segment) < 2:
            break

        url = google_maps_url(
            origin=segment[0],
            waypoints=segment[1:-1],
            destination=segment[-1],
            avoid_tolls=avoid_tolls,
            navigation=False,
        )

        links.append(
            {
                "number": number,
                "origin": segment[0],
                "destination": segment[-1],
                "url": url,
            }
        )

        start += len(segment) - 1
        number += 1

    return links


# =========================================================
# LINKS DE NAVEGAÇÃO
# =========================================================

def build_navigation_links(
    origin,
    ordered_clients,
    round_trip,
    avoid_tolls,
):
    if not ordered_clients:
        return []

    points = [origin["formatted"]]

    points.extend(
        client["formatted"]
        for client in ordered_clients
    )

    if round_trip:
        points.append(origin["formatted"])

    links = []

    # Mobile:
    # origem + 3 waypoints + destino
    max_points = 5

    start = 0
    number = 1

    while start < len(points) - 1:
        segment = points[start:start + max_points]

        if len(segment) < 2:
            break

        url = google_maps_url(
            origin=segment[0],
            waypoints=segment[1:-1],
            destination=segment[-1],
            avoid_tolls=avoid_tolls,
            navigation=True,
        )

        links.append(
            {
                "number": number,
                "origin": segment[0],
                "destination": segment[-1],
                "stops": len(segment) - 1,
                "url": url,
            }
        )

        # Último ponto deste segmento
        # passa a origem do seguinte
        start += len(segment) - 1
        number += 1

    return links


# =========================================================
# INTERFACE
# =========================================================

origin_input = st.text_input(
    "📍 Ponto de partida",
    value=(
        "Minho Jantes, Rua Da Tomada 13, "
        "4730-325 Oleiros, Vila Verde"
    ),
)

col_mode, col_tolls = st.columns(2)

with col_mode:
    mode = st.radio(
        "Tipo de rota",
        [
            "Rota aberta",
            "Ida e volta",
        ],
    )

with col_tolls:
    toll_mode = st.radio(
        "Portagens",
        [
            "🚫 Evitar portagens",
            "🛣️ Portagens permitidas",
        ],
    )

round_trip = mode == "Ida e volta"
avoid_tolls = toll_mode.startswith("🚫")


st.markdown("### Clientes")

default_clients = """Matriz Auto Braga, R. Cidade do Porto 62, 4705-084 Braga
Braga Retail Park, Loja K, Lugar De Passos E Lameiras, 4710-426 Braga
Centro Comercial Nova Arcada, Avenida De Lamas 100 Loja R 05.A, 4700-068 Braga
Av. Antonio Sergio 508, 4730-709 Vila Verde
C.C. Minho Center 59, Av. Robert Smith - Fraião, 4715-249 Braga
Centro Empresarial de Braga, Largo da Misericordia, Pav W2/W3, 4705-319 Braga
Tesla Center Porto, Av. Fontes Pereira de Melo 318, 4100-259 Porto
Av. da Independência 1 1C, 4705-162 Braga
Travessa Marceliano de Araújo 49, Ferreiros, 4705-101 Braga
Av. Barros e Soares 130, 4715-214 Braga
BMcar Braga, N101, 4715-213 Braga"""

clients_input = st.text_area(
    "Uma morada por linha",
    value=default_clients,
    height=280,
)


# =========================================================
# OTIMIZAR
# =========================================================

if st.button(
    "🚀 Otimizar rota",
    type="primary",
    use_container_width=True,
):

    client_addresses = [
        line.strip()
        for line in clients_input.splitlines()
        if line.strip()
    ]

    if not origin_input.strip():
        st.error("Indica o ponto de partida.")
        st.stop()

    if not client_addresses:
        st.error("Adiciona pelo menos um cliente.")
        st.stop()

    try:

        with st.spinner("A localizar a origem..."):
            origin = geocode_address(origin_input)

        clients = []

        progress = st.progress(0)

        for i, address in enumerate(client_addresses):

            clients.append(
                geocode_address(address)
            )

            progress.progress(
                (i + 1) / len(client_addresses)
            )

        progress.empty()

        with st.spinner("A otimizar a rota..."):

            result = optimize_route(
                origin,
                clients,
                round_trip=round_trip,
                avoid_tolls=avoid_tolls,
            )

        routes = result.get("routes", [])

        if not routes:
            st.error("Não foi encontrada uma rota.")
            st.stop()

        route = routes[0]

        visits = route.get("visits", [])

        ordered_clients = []

        for visit in visits:

            shipment_index = visit.get(
                "shipmentIndex"
            )

            if shipment_index is not None:

                ordered_clients.append(
                    clients[shipment_index]
                )

        st.session_state.route_data = {
            "origin": origin,
            "clients": clients,
            "ordered_clients": ordered_clients,
            "round_trip": round_trip,
            "avoid_tolls": avoid_tolls,
            "route": route,
        }

        st.session_state.manual_order = [
            client["original"]
            for client in ordered_clients
        ]

        st.session_state.validated = False

        st.rerun()

    except Exception as error:

        st.error("Ocorreu um erro.")
        st.code(str(error))


# =========================================================
# RESULTADO
# =========================================================

if st.session_state.route_data:

    data = st.session_state.route_data

    st.divider()

    st.markdown(
        "## 1. Resultado da otimização"
    )

    route = data["route"]

    metrics = route.get(
        "metrics",
        {},
    )

    total_km = (
        metrics.get(
            "travelDistanceMeters",
            0,
        )
        / 1000
    )

    total_time = duration_seconds(
        metrics.get(
            "travelDuration",
            "0s",
        )
    )

    c1, c2, c3 = st.columns(3)

    c1.metric(
        "Distância",
        f"{total_km:.1f} km",
    )

    c2.metric(
        "Tempo em viagem",
        format_duration(total_time),
    )

    c3.metric(
        "Clientes",
        len(data["ordered_clients"]),
    )

    if data["avoid_tolls"]:

        st.caption(
            "🚫 Rota sem portagens"
        )

    else:

        st.caption(
            "🛣️ Portagens permitidas"
        )


    # =====================================================
    # PREVIEW
    # =====================================================

    st.markdown(
        "### 🗺️ Visualizar rota sugerida"
    )

    preview_links = build_preview_links(
        data["origin"],
        data["ordered_clients"],
        data["round_trip"],
        data["avoid_tolls"],
    )

    for link in preview_links:

        text = (
            "🗺️ Abrir rota sugerida no Google Maps"
            if len(preview_links) == 1
            else
            f"🗺️ Ver rota sugerida {link['number']}"
        )

        st.link_button(
            text,
            link["url"],
            use_container_width=True,
        )


    # =====================================================
    # REVISÃO
    # =====================================================

    st.divider()

    st.markdown(
        "## 2. Rever e ajustar"
    )

    st.info(
        "Altera a posição de um cliente "
        "caso a sequência sugerida não faça sentido."
    )

    ordered_names = (
        st.session_state.manual_order.copy()
    )

    edited_rows = []

    for i, client_name in enumerate(
        ordered_names,
        start=1,
    ):

        col1, col2 = st.columns(
            [1, 5]
        )

        with col1:

            new_position = st.number_input(
                "Pos.",
                min_value=1,
                max_value=len(ordered_names),
                value=i,
                step=1,
                key=f"position_{i}_{client_name}",
                label_visibility="collapsed",
            )

        with col2:

            st.write(
                f"**{i}. {client_name}**"
            )

        edited_rows.append(
            {
                "client": client_name,
                "position": int(new_position),
                "original_position": i,
            }
        )


    if st.button(
        "🔄 Aplicar nova ordem",
        use_container_width=True,
    ):

        edited_rows.sort(
            key=lambda x: (
                x["position"],
                x["original_position"],
            )
        )

        st.session_state.manual_order = [
            row["client"]
            for row in edited_rows
        ]

        st.session_state.validated = False

        st.rerun()


    # =====================================================
    # ORDEM ATUAL
    # =====================================================

    client_lookup = {
        client["original"]: client
        for client in data["clients"]
    }

    current_clients = [
        client_lookup[name]
        for name
        in st.session_state.manual_order
    ]

    st.markdown("### Ordem atual")

    for i, client_name in enumerate(
        st.session_state.manual_order,
        start=1,
    ):

        st.write(
            f"**{i}.** {client_name}"
        )


    # =====================================================
    # PREVIEW APÓS AJUSTES
    # =====================================================

    adjusted_preview = build_preview_links(
        data["origin"],
        current_clients,
        data["round_trip"],
        data["avoid_tolls"],
    )

    st.markdown(
        "### 🗺️ Visualizar ordem atual"
    )

    for link in adjusted_preview:

        text = (
            "🗺️ Abrir ordem atual no Google Maps"
            if len(adjusted_preview) == 1
            else
            f"🗺️ Ver ordem atual {link['number']}"
        )

        st.link_button(
            text,
            link["url"],
            use_container_width=True,
        )


    # =====================================================
    # VALIDAR
    # =====================================================

    st.divider()

    st.markdown(
        "## 3. Validar rota"
    )

    if st.button(
        "✅ VALIDAR ROTA",
        type="primary",
        use_container_width=True,
    ):

        st.session_state.validated = True

        st.rerun()


# =========================================================
# LINKS FINAIS DE NAVEGAÇÃO
# =========================================================

if (
    st.session_state.route_data
    and st.session_state.validated
):

    data = st.session_state.route_data

    client_lookup = {
        client["original"]: client
        for client in data["clients"]
    }

    final_clients = [
        client_lookup[name]
        for name
        in st.session_state.manual_order
    ]

    st.divider()

    st.markdown(
        "## 4. Links para o motorista"
    )

    st.success(
        "✅ Rota validada. "
        "Estes links estão preparados "
        "para abrir a navegação no Google Maps."
    )

    navigation_links = (
        build_navigation_links(
            data["origin"],
            final_clients,
            data["round_trip"],
            data["avoid_tolls"],
        )
    )

    for link in navigation_links:

        st.markdown(
            f"### 🚚 Parte {link['number']}"
        )

        st.caption(
            f"{link['origin']} → "
            f"{link['destination']}"
        )

        st.link_button(
            (
                f"▶️ Iniciar navegação "
                f"— Parte {link['number']}"
            ),
            link["url"],
            use_container_width=True,
            type="primary",
        )

        st.code(
            link["url"],
            language=None,
        )

    st.info(
        f"Esta rota foi dividida em "
        f"{len(navigation_links)} parte(s). "
        "Quando terminar uma parte, "
        "o motorista abre o link seguinte."
    )
