import json
import html
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode

import requests
import streamlit as st
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import service_account
from streamlit_js_eval import get_geolocation
from streamlit_sortables import sort_items


# =========================================================
# CONFIGURAÇÃO
# =========================================================

st.set_page_config(
    page_title="Route Optimizer",
    page_icon="🚚",
    layout="centered",
    initial_sidebar_state="collapsed",
)


# =========================================================
# VENDEDORES
# =========================================================
# Depois trocamos isto pelos vendedores reais.

SELLERS = {
    "Vendedor Teste": (
        "Minho Jantes, Rua Da Tomada 13, "
        "4730-325 Oleiros, Vila Verde"
    ),
}


# =========================================================
# CSS MOBILE
# =========================================================

st.markdown(
    """
    <style>

    .block-container {
        max-width: 760px;
        padding-top: 1rem;
        padding-bottom: 5rem;
        padding-left: 1rem;
        padding-right: 1rem;
    }

    h1 {
        font-size: 1.9rem !important;
        margin-bottom: 0 !important;
    }

    h2 {
        font-size: 1.35rem !important;
    }

    h3 {
        font-size: 1.08rem !important;
    }

    div.stButton > button,
    div.stLinkButton > a {
        min-height: 52px;
        border-radius: 12px;
        font-size: 16px;
        font-weight: 700;
        width: 100%;
    }

    div[data-testid="stTextInput"] input,
    div[data-testid="stNumberInput"] input {
        min-height: 48px;
        border-radius: 10px;
        font-size: 16px;
    }

    div[data-testid="stTextArea"] textarea {
        border-radius: 10px;
        font-size: 16px;
    }

    div[data-baseweb="select"] > div {
        min-height: 48px;
        border-radius: 10px;
    }

    .route-card {
        padding: 15px 16px;
        margin: 8px 0;
        border-radius: 14px;
        border: 1px solid rgba(128,128,128,0.22);
    }

    .route-number {
        font-size: 12px;
        opacity: 0.65;
        margin-bottom: 4px;
        font-weight: 700;
    }

    .route-address {
        font-weight: 600;
        font-size: 15px;
        line-height: 1.4;
    }

    .comparison-card {
        border: 1px solid rgba(128,128,128,0.25);
        border-radius: 16px;
        padding: 17px;
        margin: 10px 0 14px 0;
    }

    .comparison-title {
        font-size: 18px;
        font-weight: 800;
        margin-bottom: 14px;
    }

    .comparison-grid {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 12px;
    }

    .comparison-label {
        font-size: 12px;
        opacity: 0.65;
        margin-bottom: 2px;
    }

    .comparison-value {
        font-size: 18px;
        font-weight: 750;
    }

    .cost-highlight {
        margin-top: 14px;
        padding-top: 12px;
        border-top: 1px solid rgba(128,128,128,0.20);
    }

    .cost-highlight .comparison-value {
        font-size: 24px;
    }

    @media (max-width: 600px) {

        .block-container {
            padding-left: 0.8rem;
            padding-right: 0.8rem;
            padding-top: 0.7rem;
        }

        h1 {
            font-size: 1.65rem !important;
        }

        .comparison-grid {
            grid-template-columns: 1fr 1fr;
        }
    }

    </style>
    """,
    unsafe_allow_html=True,
)


# =========================================================
# CABEÇALHO
# =========================================================

st.title("🚚 Route Optimizer")
st.caption("Planeia. Compara. Otimiza. Navega.")


# =========================================================
# SESSION STATE
# =========================================================

defaults = {
    "route_data": None,
    "validated": False,
    "manual_order": None,
    "current_location": None,
    "route_comparison": None,
    "comparison_order": None,
    "final_avoid_tolls": None,
}

for key, value in defaults.items():
    if key not in st.session_state:
        st.session_state[key] = value


# =========================================================
# GOOGLE AUTH
# =========================================================

@st.cache_resource
def get_credentials():

    info = json.loads(
        st.secrets["GCP_SERVICE_ACCOUNT_JSON"]
    )

    return service_account.Credentials.from_service_account_info(
        info,
        scopes=[
            "https://www.googleapis.com/auth/cloud-platform"
        ],
    )


def get_access_token():

    credentials = get_credentials()

    if not credentials.valid:
        credentials.refresh(
            GoogleAuthRequest()
        )

    return credentials.token


def auth_headers():

    return {
        "Authorization": f"Bearer {get_access_token()}",
        "Content-Type": "application/json",
    }


def get_project_id():

    info = json.loads(
        st.secrets["GCP_SERVICE_ACCOUNT_JSON"]
    )

    return info["project_id"]


# =========================================================
# GEOCODING
# =========================================================

@st.cache_data(show_spinner=False)
def geocode_address(address):

    address = address.strip()

    if not address:
        raise ValueError("Morada vazia.")

    encoded_address = quote(
        address,
        safe="",
    )

    url = (
        "https://geocode.googleapis.com/v4/"
        "geocode/address/"
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
            f"{response.status_code} - "
            f"{response.text}"
        )

    results = response.json().get(
        "results",
        [],
    )

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

def current_location_object(
    latitude,
    longitude,
):

    return {
        "original": "Localização atual",
        "formatted": f"{latitude},{longitude}",
        "place_id": None,
        "latitude": latitude,
        "longitude": longitude,
    }


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


def money_to_float(money):

    if not money:
        return 0

    units = float(
        money.get("units", 0)
    )

    nanos = float(
        money.get("nanos", 0)
    )

    return units + nanos / 1_000_000_000


def euro(value):

    return (
        f"{value:.2f} €"
        .replace(".", ",")
    )


# =========================================================
# ROUTE OPTIMIZATION API
# =========================================================

def optimize_route(
    origin,
    clients,
    round_trip=False,
    avoid_tolls=True,
):

    project_id = get_project_id()

    url = (
        "https://routeoptimization."
        "googleapis.com/v1/"
        f"projects/{project_id}:optimizeTours"
    )

    now = datetime.now(
        timezone.utc
    )

    start_time = now.replace(
        microsecond=0
    )

    end_time = (
        start_time
        + timedelta(hours=24)
    )

    shipments = []

    for client in clients:

        shipments.append(
            {
                "label": client["original"],

                "deliveries": [
                    {
                        "arrivalWaypoint":
                            waypoint(client),

                        "duration": "0s",

                        "label":
                            client["original"],
                    }
                ],

                "penaltyCost": 1000000,
            }
        )

    vehicle = {
        "label": "Viatura 1",

        "startWaypoint":
            waypoint(origin),

        "travelMode":
            "DRIVING",

        "routeModifiers": {
            "avoidTolls": avoid_tolls,
            "avoidHighways": False,
            "avoidFerries": True,
        },

        "costPerTraveledHour": 1.0,
    }

    if round_trip:

        vehicle["endWaypoint"] = (
            waypoint(origin)
        )

    body = {
        "timeout": "30s",

        "considerRoadTraffic":
            True,

        "model": {

            "shipments":
                shipments,

            "vehicles":
                [vehicle],

            "globalStartTime":
                start_time
                .isoformat()
                .replace("+00:00", "Z"),

            "globalEndTime":
                end_time
                .isoformat()
                .replace("+00:00", "Z"),
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
            "Erro Route Optimization API:"
            f"\n\n{response.status_code}"
            f"\n{response.text}"
        )

    return response.json()


# =========================================================
# ROUTES API — COMPARAÇÃO DE CUSTOS
# =========================================================

def compute_fixed_route(
    origin,
    ordered_clients,
    round_trip,
    avoid_tolls,
    emission_type="DIESEL",
):

    if not ordered_clients:
        raise ValueError(
            "Não existem clientes para calcular a rota."
        )

    # -----------------------------------------------------
    # ROTA ABERTA
    # origem -> clientes -> último cliente
    # -----------------------------------------------------

    if not round_trip:

        destination = ordered_clients[-1]

        intermediates = (
            ordered_clients[:-1]
        )

    # -----------------------------------------------------
    # IDA E VOLTA
    # origem -> clientes -> origem
    # -----------------------------------------------------

    else:

        destination = origin

        intermediates = (
            ordered_clients
        )

    body = {
        "origin":
            waypoint(origin),

        "destination":
            waypoint(destination),

        "intermediates": [
            waypoint(client)
            for client in intermediates
        ],

        "travelMode":
            "DRIVE",

        "routingPreference":
            "TRAFFIC_AWARE",

        "routeModifiers": {
            "avoidTolls":
                avoid_tolls,

            "avoidHighways":
                False,

            "avoidFerries":
                True,

            "vehicleInfo": {
                "emissionType":
                    emission_type
            },
        },

        "extraComputations": [
            "TOLLS"
        ],

        "languageCode":
            "pt-PT",

        "units":
            "METRIC",
    }

    headers = auth_headers()

    headers["X-Goog-FieldMask"] = (
        "routes.distanceMeters,"
        "routes.duration,"
        "routes.travelAdvisory.tollInfo"
    )

    headers["X-Goog-User-Project"] = (
        get_project_id()
    )

    response = requests.post(
        "https://routes.googleapis.com/"
        "directions/v2:computeRoutes",
        headers=headers,
        json=body,
        timeout=45,
    )

    if response.status_code != 200:

        raise RuntimeError(
            "Erro Routes API:"
            f"\n\n{response.status_code}"
            f"\n{response.text}"
        )

    result = response.json()

    routes = result.get(
        "routes",
        [],
    )

    if not routes:

        raise RuntimeError(
            "A Routes API não devolveu nenhuma rota."
        )

    route = routes[0]

    distance_m = route.get(
        "distanceMeters",
        0,
    )

    duration_s = duration_seconds(
        route.get(
            "duration",
            "0s",
        )
    )

    toll_info = (
        route
        .get("travelAdvisory", {})
        .get("tollInfo")
    )

    toll_known = True
    toll_cost = 0
    toll_currency = "EUR"
    contains_tolls = False

    if toll_info:

        contains_tolls = True

        prices = toll_info.get(
            "estimatedPrice",
            [],
        )

        if prices:

            toll_cost = sum(
                money_to_float(price)
                for price in prices
            )

            toll_currency = prices[0].get(
                "currencyCode",
                "EUR",
            )

        else:

            # Google sabe que existem portagens,
            # mas não tem estimativa de preço.
            toll_known = False

    return {
        "distance_m":
            distance_m,

        "distance_km":
            distance_m / 1000,

        "duration_s":
            duration_s,

        "toll_cost":
            toll_cost,

        "toll_known":
            toll_known,

        "contains_tolls":
            contains_tolls,

        "toll_currency":
            toll_currency,

        "avoid_tolls":
            avoid_tolls,
    }


def add_operational_costs(
    route,
    consumption,
    fuel_price,
    driver_hour_cost,
):

    distance_km = (
        route["distance_km"]
    )

    duration_h = (
        route["duration_s"]
        / 3600
    )

    fuel_litres = (
        distance_km
        * consumption
        / 100
    )

    fuel_cost = (
        fuel_litres
        * fuel_price
    )

    driver_cost = (
        duration_h
        * driver_hour_cost
    )

    route["fuel_litres"] = (
        fuel_litres
    )

    route["fuel_cost"] = (
        fuel_cost
    )

    route["driver_cost"] = (
        driver_cost
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
):

    with_tolls = compute_fixed_route(
        origin=origin,
        ordered_clients=ordered_clients,
        round_trip=round_trip,
        avoid_tolls=False,
        emission_type=emission_type,
    )

    without_tolls = compute_fixed_route(
        origin=origin,
        ordered_clients=ordered_clients,
        round_trip=round_trip,
        avoid_tolls=True,
        emission_type=emission_type,
    )

    with_tolls = add_operational_costs(
        with_tolls,
        consumption,
        fuel_price,
        driver_hour_cost,
    )

    without_tolls = add_operational_costs(
        without_tolls,
        consumption,
        fuel_price,
        driver_hour_cost,
    )

    return {
        "with_tolls":
            with_tolls,

        "without_tolls":
            without_tolls,
    }


# =========================================================
# GOOGLE MAPS URL
# =========================================================

def google_maps_url(
    origin=None,
    waypoints=None,
    destination=None,
    avoid_tolls=True,
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
        params["waypoints"] = (
            "|".join(waypoints)
        )

    if avoid_tolls:
        params["avoid"] = "tolls"

    if navigation:
        params["dir_action"] = (
            "navigate"
        )

    return (
        "https://www.google.com/maps/dir/?"
        + urlencode(
            params,
            safe="|,",
        )
    )


# =========================================================
# PREVIEW GOOGLE MAPS
# =========================================================

def build_preview_links(
    origin,
    ordered_clients,
    round_trip,
    avoid_tolls,
):

    if not ordered_clients:
        return []

    points = [
        origin["formatted"]
    ]

    points.extend(
        client["formatted"]
        for client in ordered_clients
    )

    if round_trip:
        points.append(
            origin["formatted"]
        )

    links = []

    # Desktop / preview:
    # origem + 9 intermediários + destino
    max_points = 11

    start = 0
    number = 1

    while start < len(points) - 1:

        segment = points[
            start:
            start + max_points
        ]

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
                "url": url,
            }
        )

        start += (
            len(segment) - 1
        )

        number += 1

    return links


# =========================================================
# LINKS FINAIS PARA MOTORISTA
# =========================================================

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

    # Mobile:
    # até 3 waypoints intermédios
    # + destino.
    #
    # A origem é deliberadamente omitida.
    # O Google Maps usa a localização atual
    # do motorista.

    points_per_link = 4

    index = 0
    number = 1

    while index < len(points):

        segment = points[
            index:
            index + points_per_link
        ]

        if not segment:
            break

        destination = (
            segment[-1]
        )

        intermediate = (
            segment[:-1]
        )

        url = google_maps_url(
            origin=None,
            waypoints=intermediate,
            destination=destination,
            avoid_tolls=avoid_tolls,
            navigation=True,
        )

        links.append(
            {
                "number":
                    number,

                "destination":
                    destination,

                "url":
                    url,
            }
        )

        index += points_per_link
        number += 1

    return links


# =========================================================
# CARD DE COMPARAÇÃO
# =========================================================

def comparison_card(
    title,
    route,
):

    toll_text = "0,00 €"

    if route["contains_tolls"]:

        if route["toll_known"]:

            if (
                route["toll_currency"]
                == "EUR"
            ):

                toll_text = euro(
                    route["toll_cost"]
                )

            else:

                toll_text = (
                    f"{route['toll_cost']:.2f} "
                    f"{route['toll_currency']}"
                )

        else:

            toll_text = (
                "Preço indisponível"
            )

    total_text = "—"

    if route["total_cost"] is not None:

        total_text = euro(
            route["total_cost"]
        )

    st.markdown(
        f"""
        <div class="comparison-card">

            <div class="comparison-title">
                {title}
            </div>

            <div class="comparison-grid">

                <div>
                    <div class="comparison-label">
                        DISTÂNCIA
                    </div>
                    <div class="comparison-value">
                        {route["distance_km"]:.1f} km
                    </div>
                </div>

                <div>
                    <div class="comparison-label">
                        TEMPO
                    </div>
                    <div class="comparison-value">
                        {format_duration(route["duration_s"])}
                    </div>
                </div>

                <div>
                    <div class="comparison-label">
                        COMBUSTÍVEL
                    </div>
                    <div class="comparison-value">
                        {euro(route["fuel_cost"])}
                    </div>
                </div>

                <div>
                    <div class="comparison-label">
                        PORTAGENS
                    </div>
                    <div class="comparison-value">
                        {toll_text}
                    </div>
                </div>

            </div>

            <div class="cost-highlight">

                <div class="comparison-label">
                    CUSTO ESTIMADO TOTAL
                </div>

                <div class="comparison-value">
                    {total_text}
                </div>

            </div>

        </div>
        """,
        unsafe_allow_html=True,
    )


# =========================================================
# CONFIGURAÇÃO DA ROTA
# =========================================================

st.markdown("## Nova rota")


# =========================================================
# VENDEDOR
# =========================================================

seller = st.selectbox(
    "👤 Vendedor",
    list(SELLERS.keys()),
)


# =========================================================
# PONTO DE PARTIDA
# =========================================================

st.markdown("### 📍 Ponto de partida")

start_mode = st.radio(
    "Onde começa esta rota?",
    [
        "🏢 Base do vendedor",
        "📱 A minha localização atual",
    ],
)

predefined_address = (
    SELLERS[seller]
)

if start_mode == "🏢 Base do vendedor":

    st.text_input(
        "Morada base",
        value=predefined_address,
        disabled=True,
    )

else:

    st.info(
        "A localização utilizada será "
        "a do dispositivo onde esta app está aberta."
    )

    location = get_geolocation()

    if location:

        if "error" in location:

            st.error(
                "Não foi possível obter "
                "a localização."
            )

        elif (
            location.get("coords")
            and location["coords"].get(
                "latitude"
            )
            is not None
        ):

            latitude = (
                location["coords"][
                    "latitude"
                ]
            )

            longitude = (
                location["coords"][
                    "longitude"
                ]
            )

            st.session_state[
                "current_location"
            ] = {
                "latitude":
                    latitude,

                "longitude":
                    longitude,
            }

            st.success(
                "✅ Localização atual obtida"
            )


# =========================================================
# TIPO DE ROTA
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
# PREFERÊNCIA PARA A OTIMIZAÇÃO
# =========================================================

st.markdown(
    "### 🛣️ Preferência de otimização"
)

optimization_toll_mode = st.radio(
    "Como deve a Google ordenar inicialmente as visitas?",
    [
        "⚡ Portagens permitidas",
        "🚫 Evitar portagens",
    ],
)

optimization_avoid_tolls = (
    optimization_toll_mode
    == "🚫 Evitar portagens"
)

st.caption(
    "Depois de otimizar, comparamos as duas "
    "alternativas para esta ordem de visitas."
)


# =========================================================
# CUSTOS DA VIATURA
# =========================================================

with st.expander(
    "💶 Custos da viatura",
    expanded=False,
):

    consumption = st.number_input(
        "Consumo médio (L/100 km)",
        min_value=0.0,
        max_value=50.0,
        value=8.5,
        step=0.1,
    )

    fuel_price = st.number_input(
        "Preço combustível (€/L)",
        min_value=0.0,
        max_value=5.0,
        value=1.70,
        step=0.01,
    )

    driver_hour_cost = st.number_input(
        "Custo do colaborador por hora (€)",
        min_value=0.0,
        max_value=100.0,
        value=0.0,
        step=1.0,
        help=(
            "Deixa 0 € se quiseres comparar "
            "apenas combustível + portagens."
        ),
    )

    emission_label = st.selectbox(
        "Tipo de motor",
        [
            "Diesel",
            "Gasolina",
            "Híbrido",
            "Elétrico",
        ],
    )

emission_map = {
    "Diesel": "DIESEL",
    "Gasolina": "GASOLINE",
    "Híbrido": "HYBRID",
    "Elétrico": "ELECTRIC",
}

emission_type = (
    emission_map[
        emission_label
    ]
)


# =========================================================
# CLIENTES
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
    height=250,
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
# OTIMIZAR
# =========================================================

if st.button(
    "✨ OTIMIZAR E COMPARAR",
    type="primary",
    use_container_width=True,
):

    if not client_addresses:

        st.error(
            "Adiciona pelo menos um cliente."
        )

        st.stop()

    try:

        # -------------------------------------------------
        # ORIGEM
        # -------------------------------------------------

        if (
            start_mode
            == "🏢 Base do vendedor"
        ):

            with st.spinner(
                "A localizar ponto de partida..."
            ):

                origin = (
                    geocode_address(
                        predefined_address
                    )
                )

        else:

            current = (
                st.session_state
                .current_location
            )

            if not current:

                st.error(
                    "Ainda não temos a localização atual. "
                    "Autoriza primeiro o acesso à localização."
                )

                st.stop()

            origin = (
                current_location_object(
                    current[
                        "latitude"
                    ],
                    current[
                        "longitude"
                    ],
                )
            )

        # -------------------------------------------------
        # GEOCODING CLIENTES
        # -------------------------------------------------

        clients = []

        progress = st.progress(0)

        for i, address in enumerate(
            client_addresses
        ):

            clients.append(
                geocode_address(
                    address
                )
            )

            progress.progress(
                (i + 1)
                / len(client_addresses)
            )

        progress.empty()

        # -------------------------------------------------
        # ROUTE OPTIMIZATION
        # -------------------------------------------------

        with st.spinner(
            "✨ A encontrar a melhor ordem de visitas..."
        ):

            result = optimize_route(
                origin,
                clients,
                round_trip=round_trip,
                avoid_tolls=
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

        optimization_route = (
            routes[0]
        )

        visits = (
            optimization_route
            .get(
                "visits",
                [],
            )
        )

        ordered_clients = []

        for visit in visits:

            shipment_index = (
                visit.get(
                    "shipmentIndex"
                )
            )

            if (
                shipment_index
                is not None
            ):

                ordered_clients.append(
                    clients[
                        shipment_index
                    ]
                )

        if not ordered_clients:

            st.error(
                "A otimização não devolveu clientes."
            )

            st.stop()

        # -------------------------------------------------
        # ROUTES API — COMPARAÇÃO
        # -------------------------------------------------

        with st.spinner(
            "💶 A comparar com e sem portagens..."
        ):

            comparison = compare_routes(
                origin=origin,
                ordered_clients=
                    ordered_clients,
                round_trip=
                    round_trip,
                consumption=
                    consumption,
                fuel_price=
                    fuel_price,
                driver_hour_cost=
                    driver_hour_cost,
                emission_type=
                    emission_type,
            )

        # -------------------------------------------------
        # GUARDAR ESTADO
        # -------------------------------------------------

        st.session_state.route_data = {
            "seller":
                seller,

            "origin":
                origin,

            "clients":
                clients,

            "ordered_clients":
                ordered_clients,

            "round_trip":
                round_trip,

            "optimization_avoid_tolls":
                optimization_avoid_tolls,

            "optimization_route":
                optimization_route,

            "consumption":
                consumption,

            "fuel_price":
                fuel_price,

            "driver_hour_cost":
                driver_hour_cost,

            "emission_type":
                emission_type,
        }

        st.session_state.manual_order = [
            client["original"]
            for client in ordered_clients
        ]

        st.session_state.route_comparison = (
            comparison
        )

        st.session_state.comparison_order = (
            st.session_state
            .manual_order.copy()
        )

        st.session_state.final_avoid_tolls = (
            optimization_avoid_tolls
        )

        st.session_state.validated = (
            False
        )

        st.rerun()

    except Exception as error:

        st.error(
            "Ocorreu um erro."
        )

        st.code(
            str(error)
        )


# =========================================================
# RESULTADO
# =========================================================

if st.session_state.route_data:

    data = (
        st.session_state
        .route_data
    )

    client_lookup = {
        client["original"]:
            client
        for client
        in data["clients"]
    }

    current_clients = [
        client_lookup[name]
        for name
        in st.session_state
        .manual_order
    ]

    st.divider()

    st.markdown(
        "## ✨ Rota otimizada"
    )

    st.caption(
        f"👤 {data['seller']} · "
        f"{len(current_clients)} clientes"
    )

    # =====================================================
    # COMPARAÇÃO
    # =====================================================

    comparison = (
        st.session_state
        .route_comparison
    )

    comparison_up_to_date = (
        st.session_state
        .comparison_order
        == st.session_state
        .manual_order
    )

    if comparison:

        st.markdown(
            "## 💶 Comparação de rotas"
        )

        st.caption(
            "Comparação para a mesma ordem "
            "de visitas."
        )

        comparison_card(
            "🛣️ Portagens permitidas",
            comparison[
                "with_tolls"
            ],
        )

        comparison_card(
            "🚫 Evitar portagens",
            comparison[
                "without_tolls"
            ],
        )

        route_a = (
            comparison[
                "with_tolls"
            ]
        )

        route_b = (
            comparison[
                "without_tolls"
            ]
        )

        # -------------------------------------------------
        # DIFERENÇAS
        # -------------------------------------------------

        time_diff = (
            route_b["duration_s"]
            - route_a["duration_s"]
        )

        km_diff = (
            route_b["distance_km"]
            - route_a["distance_km"]
        )

        st.markdown(
            "### Diferença"
        )

        if time_diff > 60:

            st.write(
                "🚫 Evitar portagens acrescenta "
                f"**{format_duration(time_diff)}**."
            )

        elif time_diff < -60:

            st.write(
                "🚫 Evitar portagens é "
                f"**{format_duration(abs(time_diff))} mais rápido**."
            )

        else:

            st.write(
                "⏱️ O tempo é praticamente igual."
            )

        if abs(km_diff) >= 0.1:

            if km_diff > 0:

                st.write(
                    "📏 Sem portagens: "
                    f"**+{km_diff:.1f} km**."
                )

            else:

                st.write(
                    "📏 Sem portagens: "
                    f"**{km_diff:.1f} km**."
                )

        if (
            route_a["total_cost"]
            is not None
            and route_b["total_cost"]
            is not None
        ):

            cost_diff = (
                route_b[
                    "total_cost"
                ]
                - route_a[
                    "total_cost"
                ]
            )

            if cost_diff < -0.01:

                st.success(
                    "💰 Evitar portagens poupa "
                    f"aproximadamente "
                    f"**{euro(abs(cost_diff))}**."
                )

            elif cost_diff > 0.01:

                st.info(
                    "💰 A opção com portagens "
                    "é aproximadamente "
                    f"**{euro(cost_diff)} mais barata** "
                    "considerando os custos configurados."
                )

            else:

                st.info(
                    "💰 O custo total estimado "
                    "é praticamente igual."
                )

        if (
            route_a["contains_tolls"]
            and not route_a["toll_known"]
        ):

            st.warning(
                "⚠️ A Google identificou portagens "
                "na rota, mas não conseguiu devolver "
                "um preço estimado. O custo total "
                "dessa alternativa não é apresentado."
            )

    # =====================================================
    # ESCOLHER ROTA
    # =====================================================

    st.markdown(
        "### Qual queres usar?"
    )

    default_index = (
        1
        if st.session_state
        .final_avoid_tolls
        else 0
    )

    final_choice = st.radio(
        "Opção final",
        [
            "🛣️ Portagens permitidas",
            "🚫 Evitar portagens",
        ],
        index=default_index,
    )

    st.session_state.final_avoid_tolls = (
        final_choice
        == "🚫 Evitar portagens"
    )

    # =====================================================
    # PREVIEW
    # =====================================================

    preview_links = build_preview_links(
        data["origin"],
        current_clients,
        data["round_trip"],
        st.session_state
        .final_avoid_tolls,
    )

    for link in preview_links:

        text = (
            "🗺️ VER ROTA NO GOOGLE MAPS"
        )

        if len(preview_links) > 1:

            text = (
                "🗺️ VER ROTA "
                f"{link['number']} "
                "NO GOOGLE MAPS"
            )

        st.link_button(
            text,
            link["url"],
            use_container_width=True,
        )

    # =====================================================
    # DRAG & DROP
    # =====================================================

    st.divider()

    st.markdown(
        "## ↕️ Ajustar visitas"
    )

    st.caption(
        "Mantém pressionado e arrasta "
        "cada cliente para mudar a ordem."
    )

    drag_items = []
    drag_lookup = {}

    for index, client_name in enumerate(
        st.session_state
        .manual_order,
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

    sortable_style = """
    .sortable-component {
        font-size: 15px;
    }

    .sortable-item {
        background: rgba(128,128,128,0.08);
        border: 1px solid rgba(128,128,128,0.25);
        border-radius: 12px;
        margin-bottom: 8px;
        padding: 15px 12px;
        cursor: grab;
        line-height: 1.35;
    }

    .sortable-item:hover {
        background: rgba(128,128,128,0.13);
    }
    """

    sorted_display = sort_items(
        drag_items,
        direction="vertical",
        custom_style=sortable_style,
    )

    if sorted_display:

        new_order = [
            drag_lookup[item]
            for item in sorted_display
        ]

        if (
            new_order
            != st.session_state
            .manual_order
        ):

            st.session_state[
                "manual_order"
            ] = new_order

            st.session_state[
                "validated"
            ] = False

            comparison_up_to_date = (
                False
            )

    # =====================================================
    # SE ALTEROU MANUALMENTE
    # =====================================================

    if not comparison_up_to_date:

        st.warning(
            "↕️ Alteraste a ordem das visitas. "
            "A comparação de custos acima ainda "
            "corresponde à ordem anterior."
        )

        if st.button(
            "💶 RECALCULAR COMPARAÇÃO",
            use_container_width=True,
        ):

            try:

                current_clients = [
                    client_lookup[name]
                    for name
                    in st.session_state
                    .manual_order
                ]

                with st.spinner(
                    "💶 A recalcular custos..."
                ):

                    comparison = compare_routes(
                        origin=
                            data["origin"],

                        ordered_clients=
                            current_clients,

                        round_trip=
                            data["round_trip"],

                        consumption=
                            data["consumption"],

                        fuel_price=
                            data["fuel_price"],

                        driver_hour_cost=
                            data[
                                "driver_hour_cost"
                            ],

                        emission_type=
                            data["emission_type"],
                    )

                st.session_state[
                    "route_comparison"
                ] = comparison

                st.session_state[
                    "comparison_order"
                ] = (
                    st.session_state
                    .manual_order.copy()
                )

                st.rerun()

            except Exception as error:

                st.error(
                    "Não foi possível recalcular."
                )

                st.code(
                    str(error)
                )

    # =====================================================
    # ORDEM ATUAL
    # =====================================================

    current_clients = [
        client_lookup[name]
        for name
        in st.session_state
        .manual_order
    ]

    st.markdown(
        "### 📋 Ordem atual"
    )

    for index, client in enumerate(
        current_clients,
        start=1,
    ):

        safe_address = html.escape(
            client["original"]
        )

        st.markdown(
            f"""
            <div class="route-card">
                <div class="route-number">
                    PARAGEM {index}
                </div>
                <div class="route-address">
                    {safe_address}
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    # =====================================================
    # PREVIEW ORDEM ATUAL
    # =====================================================

    current_preview = build_preview_links(
        data["origin"],
        current_clients,
        data["round_trip"],
        st.session_state
        .final_avoid_tolls,
    )

    for link in current_preview:

        text = (
            "🗺️ PRÉ-VISUALIZAR ORDEM ATUAL"
        )

        if len(current_preview) > 1:

            text = (
                "🗺️ PRÉ-VISUALIZAR "
                f"PARTE {link['number']}"
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

    comparison_is_current = (
        st.session_state
        .comparison_order
        == st.session_state
        .manual_order
    )

    if not comparison_is_current:

        st.info(
            "Recalcula a comparação antes "
            "de validar a rota."
        )

    elif not st.session_state.validated:

        if st.button(
            "✅ VALIDAR ROTA",
            type="primary",
            use_container_width=True,
        ):

            st.session_state[
                "validated"
            ] = True

            st.rerun()


# =========================================================
# ROTA VALIDADA
# =========================================================

if (
    st.session_state.route_data
    and st.session_state.validated
):

    data = (
        st.session_state
        .route_data
    )

    client_lookup = {
        client["original"]:
            client
        for client
        in data["clients"]
    }

    final_clients = [
        client_lookup[name]
        for name
        in st.session_state
        .manual_order
    ]

    st.divider()

    st.markdown(
        "## ✅ Rota pronta"
    )

    st.success(
        "Rota validada e pronta para enviar."
    )

    if (
        st.session_state
        .final_avoid_tolls
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
            st.session_state
            .final_avoid_tolls,
        )
    )

    for link in navigation_links:

        st.markdown(
            f"### 🚚 Parte "
            f"{link['number']}"
        )

        st.caption(
            "Começa na localização atual "
            "do telemóvel do motorista."
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
        st.session_state.final_avoid_tolls = None
        st.session_state.validated = False

        st.rerun()
