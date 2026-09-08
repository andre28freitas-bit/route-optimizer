import json
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode

import pandas as pd
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
st.caption("Planeamento, revisão e geração de rotas para Google Maps")


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

    credentials = service_account.Credentials.from_service_account_info(
        info,
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )

    return credentials


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

    if not address:
        raise ValueError("Morada vazia.")

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

    data = response.json()
    results = data.get("results", [])

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
# OTIMIZAÇÃO AUTOMÁTICA
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
            "globalStartTime": start_time.isoformat().replace("+00:00", "Z"),
            "globalEndTime": end_time.isoformat().replace("+00:00", "Z"),
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
# GOOGLE MAPS LINKS
# =========================================================

def google_maps_url(
    origin,
    stops,
    destination,
    avoid_tolls=True,
):
    params = {
        "api": "1",
        "origin": origin,
        "destination": destination,
        "travelmode": "driving",
    }

    if avoid_tolls:
        params["avoid"] = "tolls"

    if stops:
        params["waypoints"] = "|".join(stops)

    return (
        "https://www.google.com/maps/dir/?"
        + urlencode(params, safe="|,")
    )


def build_maps_links(
    origin,
    ordered_clients,
    round_trip,
    avoid_tolls,
):
    if not ordered_clients:
        return []

    all_points = [origin["formatted"]]

    all_points.extend(
        client["formatted"]
        for client in ordered_clients
    )

    if round_trip:
        all_points.append(origin["formatted"])

    links = []

    # Conservador para garantir boa compatibilidade.
    # Cada bloco tem origem + até 9 waypoints + destino.
    max_points_per_segment = 11

    start_index = 0
    segment_number = 1

    while start_index < len(all_points) - 1:
        segment_points = all_points[
            start_index:
            start_index + max_points_per_segment
        ]

        if len(segment_points) < 2:
            break

        segment_origin = segment_points[0]
        segment_destination = segment_points[-1]
        segment_waypoints = segment_points[1:-1]

        url = google_maps_url(
            segment_origin,
            segment_waypoints,
            segment_destination,
            avoid_tolls=avoid_tolls,
        )

        links.append(
            {
                "number": segment_number,
                "origin": segment_origin,
                "destination": segment_destination,
                "url": url,
            }
        )

        start_index += len(segment_points) - 1
        segment_number += 1

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
            with st.spinner(
                f"A localizar cliente "
                f"{i + 1}/{len(client_addresses)}..."
            ):
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
        transitions = route.get("transitions", [])

        ordered_clients = []

        for visit in visits:
            shipment_index = visit.get("shipmentIndex")

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
            "transitions": transitions,
        }

        st.session_state.manual_order = [
            client["original"]
            for client in ordered_clients
        ]

        st.session_state.validated = False

    except Exception as error:
        st.error("Ocorreu um erro.")
        st.code(str(error))


# =========================================================
# REVISÃO
# =========================================================

if st.session_state.route_data:
    data = st.session_state.route_data

    st.divider()
    st.markdown("## 1. Resultado da otimização")

    route = data["route"]
    metrics = route.get("metrics", {})

    total_km = (
        metrics.get("travelDistanceMeters", 0)
        / 1000
    )

    total_time = duration_seconds(
        metrics.get("travelDuration", "0s")
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
        st.caption("🚫 Rota configurada para evitar portagens")
    else:
        st.caption("🛣️ Portagens permitidas")

    st.markdown("## 2. Rever e ajustar a ordem")

    st.info(
        "Se a ordem não estiver correta, altera a posição "
        "dos clientes abaixo. Só depois valida a rota."
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

    st.markdown("### Ordem atual")

    for i, client_name in enumerate(
        st.session_state.manual_order,
        start=1,
    ):
        st.write(
            f"**{i}.** {client_name}"
        )

    st.divider()

    st.markdown("## 3. Validar rota")

    if st.button(
        "✅ VALIDAR ROTA",
        type="primary",
        use_container_width=True,
    ):
        st.session_state.validated = True
        st.rerun()


# =========================================================
# LINKS FINAIS
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
        for name in st.session_state.manual_order
    ]

    st.divider()
    st.markdown("## 4. Rota validada")

    st.success(
        "✅ Rota validada. "
        "Os links abaixo já estão prontos para enviar."
    )

    links = build_maps_links(
        data["origin"],
        final_clients,
        data["round_trip"],
        data["avoid_tolls"],
    )

    for link in links:
        st.markdown(
            f"### 🗺️ Rota {link['number']}"
        )

        st.caption(
            f"{link['origin']} → "
            f"{link['destination']}"
        )

        st.link_button(
            f"📍 Abrir Rota {link['number']} no Google Maps",
            link["url"],
            use_container_width=True,
            type="primary",
        )

        st.code(
            link["url"],
            language=None,
        )

    st.caption(
        f"Foram gerados {len(links)} link(s) "
        "para esta rota."
    )
