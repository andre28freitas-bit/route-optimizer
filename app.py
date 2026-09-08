import json
import math
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode

import requests
import streamlit as st
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import service_account
from streamlit_js_eval import get_geolocation
from streamlit_sortables import sort_items
from toll_estimator import (
    estimate_portuguese_tolls,
    estimate_tolls_from_google_steps,
    DEFAULT_TOLL_RATE_PER_KM,
    TOLL_MODEL_VERSION,
)


# =========================================================
# APP CONFIG
# =========================================================

st.set_page_config(
    page_title="Route Optimizer",
    page_icon="🚚",
    layout="centered",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
    <style>
    .block-container {
        max-width: 820px;
        padding-top: 1rem;
        padding-bottom: 5rem;
        padding-left: 1rem;
        padding-right: 1rem;
    }

    div.stButton > button,
    div.stLinkButton > a {
        min-height: 50px;
        border-radius: 12px;
        font-size: 16px;
        font-weight: 700;
        width: 100%;
    }

    div[data-testid="stTextInput"] input,
    div[data-testid="stNumberInput"] input {
        min-height: 46px;
        font-size: 16px;
    }

    div[data-testid="stTextArea"] textarea {
        font-size: 16px;
    }

    @media (max-width: 600px) {
        .block-container {
            padding-left: 0.8rem;
            padding-right: 0.8rem;
            padding-top: 0.7rem;
        }
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# =========================================================
# SELLERS
# =========================================================

SELLERS = {
    "Vendedor Teste": (
        "Minho Jantes, Rua Da Tomada 13, "
        "4730-325 Oleiros, Vila Verde"
    ),
}


# =========================================================
# SESSION STATE
# =========================================================

DEFAULTS = {
    "route_data": None,
    "manual_order": None,
    "route_comparison": None,
    "comparison_order": None,
    "final_avoid_tolls": False,
    "validated": False,
    "current_location": None,
}

for key, value in DEFAULTS.items():
    if key not in st.session_state:
        st.session_state[key] = value


# =========================================================
# AUTH
# =========================================================

@st.cache_resource
def get_credentials():
    info = json.loads(st.secrets["GCP_SERVICE_ACCOUNT_JSON"])

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


def get_project_id():
    info = json.loads(st.secrets["GCP_SERVICE_ACCOUNT_JSON"])
    return info["project_id"]


# =========================================================
# HELPERS
# =========================================================

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


def euro(value):
    return f"{value:.2f} €".replace(".", ",")


def money_to_float(money):
    units = float(money.get("units", 0))
    nanos = float(money.get("nanos", 0))
    return units + nanos / 1_000_000_000


def waypoint(location):
    return {
        "location": {
            "latLng": {
                "latitude": location["latitude"],
                "longitude": location["longitude"],
            }
        }
    }


def current_location_object(latitude, longitude):
    return {
        "original": "Localização atual",
        "formatted": f"{latitude},{longitude}",
        "latitude": latitude,
        "longitude": longitude,
        "place_id": None,
    }


def decode_polyline(encoded):
    """Decode a Google encoded polyline into [[lon, lat], ...] for PyDeck."""
    if not encoded:
        return []

    coordinates = []
    index = 0
    lat = 0
    lng = 0

    while index < len(encoded):
        result = 0
        shift = 0

        while True:
            b = ord(encoded[index]) - 63
            index += 1
            result |= (b & 0x1F) << shift
            shift += 5
            if b < 0x20:
                break

        delta_lat = ~(result >> 1) if result & 1 else result >> 1
        lat += delta_lat

        result = 0
        shift = 0

        while True:
            b = ord(encoded[index]) - 63
            index += 1
            result |= (b & 0x1F) << shift
            shift += 5
            if b < 0x20:
                break

        delta_lng = ~(result >> 1) if result & 1 else result >> 1
        lng += delta_lng

        coordinates.append(
            [
                lng / 1e5,
                lat / 1e5,
            ]
        )

    return coordinates


# =========================================================
# GEOCODING
# =========================================================

@st.cache_data(show_spinner=False)
def geocode_address(address):
    address = address.strip()

    if not address:
        raise ValueError("Morada vazia.")

    encoded = quote(address, safe="")

    url = (
        "https://geocode.googleapis.com/v4/"
        f"geocode/address/{encoded}"
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
            f"Erro Geocoding API ({response.status_code}): "
            f"{response.text}"
        )

    results = response.json().get("results", [])

    if not results:
        raise ValueError(
            f"Não foi possível localizar: {address}"
        )

    result = results[0]

    return {
        "original": address,
        "formatted": result.get("formattedAddress", address),
        "place_id": result.get("placeId"),
        "latitude": result["location"]["latitude"],
        "longitude": result["location"]["longitude"],
    }


# =========================================================
# ROUTE OPTIMIZATION API
# =========================================================

def optimize_route(
    origin,
    clients,
    round_trip,
    avoid_tolls,
):
    url = (
        "https://routeoptimization.googleapis.com/v1/"
        f"projects/{get_project_id()}:optimizeTours"
    )

    start_time = datetime.now(timezone.utc).replace(microsecond=0)
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
            f"Erro Route Optimization API ({response.status_code}): "
            f"{response.text}"
        )

    return response.json()


# =========================================================
# PORTAGENS — GEOMETRIA OFICIAL IP
# =========================================================

# A estimativa é feita em toll_estimator.py a partir da polyline Google e da
# camada pública da Infraestruturas de Portugal, que marca os troços com
# portagem. Assim não cobramos automaticamente todos os km de uma autoestrada.


# =========================================================
# ROUTES API
# =========================================================

def compute_fixed_route(
    origin,
    ordered_clients,
    round_trip,
    avoid_tolls,
    emission_type,
    toll_vehicle_class=1,
):
    if not ordered_clients:
        raise ValueError("Não existem clientes para calcular.")

    if round_trip:
        destination = origin
        intermediates = ordered_clients
    else:
        destination = ordered_clients[-1]
        intermediates = ordered_clients[:-1]

    body = {
        "origin": waypoint(origin),
        "destination": waypoint(destination),
        "intermediates": [waypoint(client) for client in intermediates],
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE",
        "routeModifiers": {
            "avoidTolls": avoid_tolls,
            "avoidHighways": False,
            "avoidFerries": True,
            "vehicleInfo": {"emissionType": emission_type},
        },
        "languageCode": "pt-PT",
        "units": "METRIC",
        "polylineQuality": "HIGH_QUALITY",
    }

    headers = auth_headers()
    headers["X-Goog-FieldMask"] = (
        "routes.distanceMeters,"
        "routes.duration,"
        "routes.polyline.encodedPolyline,"
        "routes.legs.steps.distanceMeters,"
        "routes.legs.steps.navigationInstruction.instructions"
    )
    headers["X-Goog-User-Project"] = get_project_id()

    response = requests.post(
        "https://routes.googleapis.com/directions/v2:computeRoutes",
        headers=headers,
        json=body,
        timeout=45,
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"Erro Routes API ({response.status_code}): {response.text}"
        )

    routes = response.json().get("routes", [])
    if not routes:
        raise RuntimeError("A Routes API não devolveu nenhuma rota.")

    route = routes[0]

    encoded_polyline = (
        route.get("polyline", {}).get("encodedPolyline", "")
    )
    route_path = decode_polyline(encoded_polyline)
    route_steps = [
        step
        for leg in route.get("legs", [])
        for step in leg.get("steps", [])
    ]

    if avoid_tolls:
        toll_cost = 0.0
        toll_known = True
        contains_tolls = False
        toll_source = "0 € — alternativa calculada para evitar portagens"
        chargeable_motorway_km = 0.0
        motorway_km = 0.0
        motorway_by_road_km = {}
    else:
        try:
            toll_estimate = estimate_portuguese_tolls(
                route_path,
                vehicle_class=toll_vehicle_class,
            )
            toll_cost = toll_estimate["cost"]
            toll_known = toll_estimate["known"]
            chargeable_motorway_km = toll_estimate["tolled_km"]
            motorway_km = chargeable_motorway_km
            motorway_by_road_km = toll_estimate["by_road_km"]
            toll_source = toll_estimate["source"]
            contains_tolls = toll_cost > 0 or chargeable_motorway_km > 0
        except Exception:
            # Se a rede oficial IP estiver temporariamente indisponível,
            # não bloqueamos a estimativa: usamos os steps da própria rota Google.
            toll_estimate = estimate_tolls_from_google_steps(
                route_steps,
                vehicle_class=toll_vehicle_class,
            )
            toll_cost = toll_estimate["cost"]
            toll_known = toll_estimate["known"]
            chargeable_motorway_km = toll_estimate["tolled_km"]
            motorway_km = chargeable_motorway_km
            motorway_by_road_km = toll_estimate["by_road_km"]
            toll_source = toll_estimate["source"]
            contains_tolls = toll_cost > 0 or chargeable_motorway_km > 0

    return {
        "distance_km": route.get("distanceMeters", 0) / 1000,
        "duration_s": duration_seconds(route.get("duration", "0s")),
        "contains_tolls": contains_tolls,
        "toll_known": toll_known,
        "toll_cost": round(toll_cost, 2),
        "toll_currency": "EUR",
        "toll_source": toll_source,
        "toll_segments": [],
        "avoid_tolls": avoid_tolls,
        "motorway_km": round(motorway_km, 1),
        "chargeable_motorway_km": round(chargeable_motorway_km, 1),
        "motorway_by_road_km": motorway_by_road_km,
        "encoded_polyline": encoded_polyline,
        "path": route_path,
    }


def add_costs(
    route,
    consumption,
    fuel_price,
    driver_hour_cost,
):
    fuel_litres = (
        route["distance_km"]
        * consumption
        / 100
    )

    fuel_cost = fuel_litres * fuel_price

    driver_cost = (
        route["duration_s"]
        / 3600
        * driver_hour_cost
    )

    route["fuel_litres"] = fuel_litres
    route["fuel_cost"] = fuel_cost
    route["driver_cost"] = driver_cost

    route["direct_cost"] = (
        fuel_cost
        + (route["toll_cost"] if route["toll_known"] else 0)
    )

    if route["toll_known"]:
        route["total_cost"] = (
            fuel_cost
            + route["toll_cost"]
            + driver_cost
        )
    else:
        route["total_cost"] = None

    return route


def compare_routes(
    origin,
    ordered_clients,
    round_trip,
    consumption,
    fuel_price,
    driver_hour_cost,
    emission_type,
    toll_vehicle_class=1,
):
    with_tolls = compute_fixed_route(
        origin,
        ordered_clients,
        round_trip,
        False,
        emission_type,
        toll_vehicle_class,
    )

    without_tolls = compute_fixed_route(
        origin,
        ordered_clients,
        round_trip,
        True,
        emission_type,
        toll_vehicle_class,
    )

    with_tolls = add_costs(
        with_tolls,
        consumption,
        fuel_price,
        driver_hour_cost,
    )

    without_tolls = add_costs(
        without_tolls,
        consumption,
        fuel_price,
        driver_hour_cost,
    )

    return {
        "with_tolls": with_tolls,
        "without_tolls": without_tolls,
    }


# =========================================================
# GOOGLE MAPS NAVIGATION LINKS
# =========================================================

def google_maps_url(
    origin=None,
    waypoints=None,
    destination=None,
    avoid_tolls=False,
    navigation=False,
):
    params = {
        "api": "1",
        "destination": destination,
        "travelmode": "driving",
    }

    if origin:
        params["origin"] = origin

    if waypoints:
        params["waypoints"] = "|".join(waypoints)

    if avoid_tolls:
        params["avoid"] = "tolls"

    if navigation:
        params["dir_action"] = "navigate"

    return (
        "https://www.google.com/maps/dir/?"
        + urlencode(
            params,
            safe="|,",
        )
    )



def build_preview_links(
    origin,
    ordered_clients,
    round_trip,
    avoid_tolls,
):
    """
    Google Maps preview with the CURRENT order.
    Google Maps URLs have waypoint limits, so larger routes are split
    only when technically necessary.
    """
    if not ordered_clients:
        return []

    destinations = [client["formatted"] for client in ordered_clients]

    if round_trip:
        destinations.append(origin["formatted"])

    # Explicit origin + up to 9 intermediate waypoints + destination.
    # This is intended as a planning preview, not turn-by-turn navigation.
    max_destinations_per_link = 10
    links = []
    cursor = 0
    segment_origin = origin["formatted"]
    part = 1

    while cursor < len(destinations):
        segment = destinations[cursor:cursor + max_destinations_per_link]

        links.append(
            {
                "number": part,
                "url": google_maps_url(
                    origin=segment_origin,
                    waypoints=segment[:-1],
                    destination=segment[-1],
                    avoid_tolls=avoid_tolls,
                    navigation=False,
                ),
            }
        )

        segment_origin = segment[-1]
        cursor += max_destinations_per_link
        part += 1

    return links


def build_navigation_links(
    ordered_clients,
    round_trip,
    return_origin,
    avoid_tolls,
):
    if not ordered_clients:
        return []

    points = [
        client["formatted"]
        for client in ordered_clients
    ]

    if round_trip:
        points.append(
            return_origin["formatted"]
        )

    links = []

    # A navegação final é pensada para abrir diretamente na app Google Maps.
    # Até 9 waypoints + destino por link.
    points_per_link = 10

    index = 0
    number = 1

    while index < len(points):
        segment = points[
            index:
            index + points_per_link
        ]

        if not segment:
            break

        links.append(
            {
                "number": number,
                "url": google_maps_url(
                    origin=None,
                    waypoints=segment[:-1],
                    destination=segment[-1],
                    avoid_tolls=avoid_tolls,
                    navigation=True,
                ),
            }
        )

        index += points_per_link
        number += 1

    return links


# =========================================================
# UI HELPERS
# =========================================================

def show_comparison_card(
    title,
    route,
):
    with st.container(border=True):
        st.subheader(title)

        col1, col2 = st.columns(2)

        with col1:
            st.metric(
                "Distância",
                f"{route['distance_km']:.1f} km",
            )

        with col2:
            st.metric(
                "Tempo",
                format_duration(route["duration_s"]),
            )

        col3, col4 = st.columns(2)

        with col3:
            st.metric(
                "Combustível",
                euro(route["fuel_cost"]),
            )

        with col4:
            if route["contains_tolls"]:
                if route["toll_known"]:
                    if route["toll_currency"] == "EUR":
                        toll_text = euro(
                            route["toll_cost"]
                        )
                    else:
                        toll_text = (
                            f"{route['toll_cost']:.2f} "
                            f"{route['toll_currency']}"
                        )
                else:
                    toll_text = "Indisponível"
            else:
                toll_text = "0,00 €"

            st.metric(
                "Portagens estimadas",
                toll_text,
            )
            if route.get("toll_known") and route.get("toll_source"):
                st.caption("Custo estimado de portagens por cada km percorrido em AE: 0,08 €")

        col5, col6 = st.columns(2)

        with col5:
            st.metric(
                "Custo direto",
                euro(route["direct_cost"]),
                help="Combustível + portagens",
            )

        with col6:
            if route["total_cost"] is not None:
                st.metric(
                    "Custo operacional",
                    euro(route["total_cost"]),
                    help=(
                        "Combustível + portagens "
                        "+ custo do colaborador"
                    ),
                )
            else:
                st.metric(
                    "Custo operacional",
                    "—",
                )


# =========================================================
# HEADER
# =========================================================

st.title("🚚 Route Optimizer")
st.caption(f"Modelo de portagens: {TOLL_MODEL_VERSION} · {DEFAULT_TOLL_RATE_PER_KM:.3f} €/km portajado")
st.caption("Planeia. Compara. Decide. Navega.")

st.markdown("## Nova rota")


# =========================================================
# SELLER
# =========================================================

seller = st.selectbox(
    "👤 Vendedor",
    list(SELLERS.keys()),
)

predefined_address = SELLERS[seller]


# =========================================================
# START
# =========================================================

st.markdown("### 📍 Ponto de partida")

start_mode = st.radio(
    "Onde começa esta rota?",
    [
        "🏢 Base do vendedor",
        "📱 A minha localização atual",
    ],
)

if start_mode == "🏢 Base do vendedor":
    st.text_input(
        "Morada base",
        value=predefined_address,
        disabled=True,
    )

else:
    st.caption(
        "Será usada a localização do dispositivo "
        "onde a app está aberta."
    )

    location = get_geolocation()

    if location and location.get("coords"):
        latitude = location["coords"].get("latitude")
        longitude = location["coords"].get("longitude")

        if (
            latitude is not None
            and longitude is not None
        ):
            st.session_state.current_location = {
                "latitude": latitude,
                "longitude": longitude,
            }

            st.success(
                "✅ Localização atual obtida"
            )


# =========================================================
# ROUTE TYPE
# =========================================================

st.markdown("### 🚚 Tipo de rota")

mode = st.radio(
    "Percurso",
    [
        "➡️ Rota aberta",
        "🔁 Ida e volta",
    ],
    horizontal=True,
)

round_trip = (
    mode == "🔁 Ida e volta"
)


# =========================================================
# INITIAL OPTIMIZATION
# =========================================================

st.markdown("### 🛣️ Preferência inicial")

optimization_choice = st.radio(
    "Como queres otimizar inicialmente?",
    [
        "⚡ Portagens permitidas",
        "🚫 Evitar portagens",
    ],
)

optimization_avoid_tolls = (
    optimization_choice
    == "🚫 Evitar portagens"
)


# =========================================================
# COSTS
# =========================================================

with st.expander(
    "💶 Custos da viatura"
):
    consumption = st.number_input(
        "Consumo médio (L/100 km)",
        min_value=0.0,
        value=8.5,
        step=0.1,
    )

    fuel_price = st.number_input(
        "Preço combustível (€/L)",
        min_value=0.0,
        value=1.70,
        step=0.01,
    )

    driver_hour_cost = st.number_input(
        "Custo do colaborador por hora (€)",
        min_value=0.0,
        value=0.0,
        step=1.0,
    )

    toll_vehicle_class = 1  # Mantido por compatibilidade; estimativa atual é por km.

    emission_label = st.selectbox(
        "Tipo de motor",
        [
            "Diesel",
            "Gasolina",
            "Híbrido",
            "Elétrico",
        ],
    )

EMISSIONS = {
    "Diesel": "DIESEL",
    "Gasolina": "GASOLINE",
    "Híbrido": "HYBRID",
    "Elétrico": "ELECTRIC",
}

emission_type = (
    EMISSIONS[emission_label]
)


# =========================================================
# CLIENTS
# =========================================================

st.markdown("### 📋 Clientes")

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
    height=240,
)

client_addresses = [
    line.strip()
    for line in clients_input.splitlines()
    if line.strip()
]

st.caption(
    f"{len(client_addresses)} cliente(s)"
)


# =========================================================
# OPTIMIZE
# =========================================================

if st.button(
    "✨ OTIMIZAR ROTA",
    type="primary",
    use_container_width=True,
):
    if not client_addresses:
        st.error(
            "Adiciona pelo menos um cliente."
        )
        st.stop()

    try:
        if (
            start_mode
            == "🏢 Base do vendedor"
        ):
            with st.spinner(
                "A localizar ponto de partida..."
            ):
                origin = geocode_address(
                    predefined_address
                )

        else:
            current = (
                st.session_state.current_location
            )

            if not current:
                st.error(
                    "Ainda não foi possível obter "
                    "a localização atual."
                )
                st.stop()

            origin = current_location_object(
                current["latitude"],
                current["longitude"],
            )

        clients = []
        progress = st.progress(0)

        for index, address in enumerate(
            client_addresses
        ):
            clients.append(
                geocode_address(address)
            )

            progress.progress(
                (index + 1)
                / len(client_addresses)
            )

        progress.empty()

        with st.spinner(
            "✨ A encontrar a melhor ordem de visitas..."
        ):
            result = optimize_route(
                origin,
                clients,
                round_trip,
                optimization_avoid_tolls,
            )

        routes = result.get(
            "routes",
            [],
        )

        if not routes:
            st.error(
                "Não foi encontrada nenhuma rota."
            )
            st.stop()

        optimization_route = routes[0]

        ordered_clients = []

        for visit in optimization_route.get(
            "visits",
            [],
        ):
            shipment_index = visit.get(
                "shipmentIndex"
            )

            if shipment_index is not None:
                ordered_clients.append(
                    clients[shipment_index]
                )

        if not ordered_clients:
            st.error(
                "A otimização não devolveu "
                "nenhuma visita."
            )
            st.stop()

        with st.spinner(
            "💶 A calcular as alternativas..."
        ):
            comparison = compare_routes(
                origin,
                ordered_clients,
                round_trip,
                consumption,
                fuel_price,
                driver_hour_cost,
                emission_type,
                toll_vehicle_class,
            )

        st.session_state.route_data = {
            "seller": seller,
            "origin": origin,
            "clients": clients,
            "round_trip": round_trip,
            "consumption": consumption,
            "fuel_price": fuel_price,
            "driver_hour_cost": (
                driver_hour_cost
            ),
            "emission_type": emission_type,
            "toll_vehicle_class": toll_vehicle_class,
        }

        st.session_state.manual_order = [
            client["original"]
            for client in ordered_clients
        ]

        st.session_state.route_comparison = (
            comparison
        )

        st.session_state.comparison_order = (
            st.session_state.manual_order.copy()
        )

        st.session_state.final_avoid_tolls = (
            optimization_avoid_tolls
        )

        st.session_state.validated = False

        st.rerun()

    except Exception as error:
        st.error(
            "Ocorreu um erro."
        )
        st.code(
            str(error)
        )


# =========================================================
# RESULTS + FULL PREVIEW
# =========================================================

if st.session_state.route_data:
    data = (
        st.session_state.route_data
    )

    client_lookup = {
        client["original"]: client
        for client in data["clients"]
    }

    current_clients = [
        client_lookup[name]
        for name in st.session_state.manual_order
    ]

    st.divider()

    st.markdown(
        "## ✨ Rota sugerida"
    )

    st.caption(
        f"👤 {data['seller']} · "
        f"{len(current_clients)} clientes"
    )

    comparison = (
        st.session_state.route_comparison
    )

    if comparison:
        st.markdown(
            "### 💶 Comparação"
        )

        show_comparison_card(
            "🛣️ Portagens permitidas",
            comparison["with_tolls"],
        )

        show_comparison_card(
            "🚫 Evitar portagens",
            comparison["without_tolls"],
        )

    st.markdown(
        "### Qual rota queres visualizar?"
    )

    choice = st.radio(
        "Alternativa",
        [
            "🛣️ Portagens permitidas",
            "🚫 Evitar portagens",
        ],
        index=(
            1
            if st.session_state.final_avoid_tolls
            else 0
        ),
    )

    st.session_state.final_avoid_tolls = (
        choice
        == "🚫 Evitar portagens"
    )

    comparison_is_current = (
        st.session_state.comparison_order
        == st.session_state.manual_order
    )

    if comparison_is_current:
        st.markdown(
            "## 🗺️ Ver ordem atual no Google Maps"
        )

        st.caption(
            "O Google Maps abre já com os pontos pela ordem atual. "
            "Se quiseres mudar a sequência, volta à app, arrasta as visitas "
            "e abre novamente o Google Maps."
        )

        preview_links = build_preview_links(
            data["origin"],
            current_clients,
            data["round_trip"],
            st.session_state.final_avoid_tolls,
        )

        for preview in preview_links:
            label = (
                "🗺️ VER ROTA NO GOOGLE MAPS"
                if len(preview_links) == 1
                else f"🗺️ VER ROTA NO GOOGLE MAPS — PARTE {preview['number']}"
            )

            st.link_button(
                label,
                preview["url"],
                use_container_width=True,
                type="primary",
            )

        if len(preview_links) > 1:
            st.caption(
                "A rota ultrapassa o limite de pontos de um único URL do Google Maps, "
                "por isso a pré-visualização foi dividida apenas onde é necessário."
            )

    else:
        st.warning(
            "Alteraste a ordem das visitas. "
            "Atualiza a rota para gerar um novo link do Google Maps "
            "com esta sequência."
        )

    st.divider()

    st.markdown(
        "## ↕️ Ajustar visitas"
    )

    st.caption(
        "Mantém pressionado e arrasta "
        "para mudar a ordem."
    )

    drag_items = []
    drag_lookup = {}

    for index, client_name in enumerate(
        st.session_state.manual_order,
        start=1,
    ):
        display = (
            f"{index}. {client_name}"
        )

        drag_items.append(
            display
        )

        drag_lookup[
            display
        ] = client_name

    sorted_display = sort_items(
        drag_items,
        direction="vertical",
    )

    if sorted_display:
        new_order = [
            drag_lookup[item]
            for item in sorted_display
        ]

        if (
            new_order
            != st.session_state.manual_order
        ):
            st.session_state.manual_order = (
                new_order
            )

            st.session_state.validated = False

            st.rerun()

    comparison_is_current = (
        st.session_state.comparison_order
        == st.session_state.manual_order
    )

    if not comparison_is_current:
        if st.button(
            "🔄 ATUALIZAR E VER NOVA ORDEM",
            type="primary",
            use_container_width=True,
        ):
            try:
                current_clients = [
                    client_lookup[name]
                    for name in st.session_state.manual_order
                ]

                with st.spinner(
                    "A atualizar rota..."
                ):
                    comparison = compare_routes(
                        data["origin"],
                        current_clients,
                        data["round_trip"],
                        data["consumption"],
                        data["fuel_price"],
                        data["driver_hour_cost"],
                        data["emission_type"],
                    )

                st.session_state.route_comparison = (
                    comparison
                )

                st.session_state.comparison_order = (
                    st.session_state.manual_order.copy()
                )

                st.rerun()

            except Exception as error:
                st.error(
                    "Não foi possível atualizar a rota."
                )

                st.code(
                    str(error)
                )

    st.markdown(
        "### 📋 Ordem atual"
    )

    for index, client in enumerate(
        current_clients,
        start=1,
    ):
        with st.container(
            border=True
        ):
            st.caption(
                f"PARAGEM {index}"
            )

            st.write(
                client["original"]
            )

    st.divider()

    comparison_is_current = (
        st.session_state.comparison_order
        == st.session_state.manual_order
    )

    if (
        comparison_is_current
        and not st.session_state.validated
    ):
        st.markdown(
            "### A ordem está correta?"
        )

        st.caption(
            "Cria o roteiro quando estiveres satisfeito com a ordem "
            "que viste no Google Maps."
        )

        if st.button(
            "✅ CRIAR ROTEIRO",
            type="primary",
            use_container_width=True,
        ):
            st.session_state.validated = True
            st.rerun()


# =========================================================
# FINAL NAVIGATION
# =========================================================

if (
    st.session_state.route_data
    and st.session_state.validated
):
    data = (
        st.session_state.route_data
    )

    client_lookup = {
        client["original"]: client
        for client in data["clients"]
    }

    final_clients = [
        client_lookup[name]
        for name in st.session_state.manual_order
    ]

    st.divider()

    st.markdown(
        "## ✅ Rota validada"
    )

    st.success(
        "Agora sim: estes links são "
        "para navegação do motorista."
    )

    if (
        st.session_state.final_avoid_tolls
    ):
        st.caption(
            "🚫 Navegação configurada "
            "para evitar portagens."
        )
    else:
        st.caption(
            "🛣️ Navegação com "
            "portagens permitidas."
        )

    navigation_links = (
        build_navigation_links(
            final_clients,
            data["round_trip"],
            data["origin"],
            st.session_state.final_avoid_tolls,
        )
    )

    for link in navigation_links:
        st.markdown(
            f"### 🚚 Parte {link['number']}"
        )

        st.caption(
            "Ao abrir no telemóvel do motorista, "
            "a navegação começa na localização atual."
        )

        st.link_button(
            (
                "▶️ INICIAR NAVEGAÇÃO "
                f"— PARTE {link['number']}"
            ),
            link["url"],
            use_container_width=True,
            type="primary",
        )

    if len(navigation_links) > 1:
        st.info(
            "Quando terminar uma parte, "
            "abre o link seguinte."
        )

    st.divider()

    if st.button(
        "➕ Criar nova rota",
        use_container_width=True,
    ):
        st.session_state.route_data = None
        st.session_state.manual_order = None
        st.session_state.route_comparison = None
        st.session_state.comparison_order = None
        st.session_state.final_avoid_tolls = False
        st.session_state.validated = False

        st.rerun()
