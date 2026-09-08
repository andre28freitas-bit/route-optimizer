import json
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
# Depois substituímos pelos 8 vendedores reais
# e respetivas moradas base.

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
        padding-bottom: 4rem;
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
        font-size: 1.1rem !important;
    }

    div.stButton > button,
    div.stLinkButton > a {
        min-height: 52px;
        border-radius: 12px;
        font-size: 16px;
        font-weight: 700;
        width: 100%;
    }

    div[data-testid="stTextInput"] input {
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

    div[data-testid="stMetric"] {
        border: 1px solid rgba(128,128,128,0.20);
        border-radius: 14px;
        padding: 12px;
    }

    .route-card {
        padding: 15px 16px;
        margin: 8px 0;
        border-radius: 14px;
        border: 1px solid rgba(128,128,128,0.22);
    }

    .route-number {
        font-size: 13px;
        opacity: 0.7;
        margin-bottom: 4px;
    }

    .route-address {
        font-weight: 600;
        font-size: 15px;
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

        div[data-testid="column"] {
            min-width: 0 !important;
        }

        div[data-testid="stMetricValue"] {
            font-size: 1.35rem;
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
st.caption("Planeia. Otimiza. Valida. Navega.")


# =========================================================
# SESSION STATE
# =========================================================

defaults = {
    "route_data": None,
    "validated": False,
    "manual_order": None,
    "current_location": None,
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
        "Authorization":
            f"Bearer {get_access_token()}",
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
            f"Não foi possível localizar: "
            f"{address}"
        )

    result = results[0]

    return {
        "original": address,
        "formatted": result.get(
            "formattedAddress",
            address,
        ),
        "place_id": result.get(
            "placeId"
        ),
        "latitude":
            result["location"]["latitude"],
        "longitude":
            result["location"]["longitude"],
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
        "formatted":
            f"{latitude},{longitude}",
        "place_id": None,
        "latitude": latitude,
        "longitude": longitude,
    }


def waypoint(location):

    return {
        "location": {
            "latLng": {
                "latitude":
                    location["latitude"],
                "longitude":
                    location["longitude"],
            }
        }
    }


def duration_seconds(value):

    if not value:
        return 0

    if (
        isinstance(value, str)
        and value.endswith("s")
    ):
        try:
            return float(
                value[:-1]
            )
        except ValueError:
            return 0

    return 0


def format_duration(seconds):

    seconds = int(
        round(seconds)
    )

    hours = seconds // 3600

    minutes = (
        seconds % 3600
    ) // 60

    if hours:
        return (
            f"{hours} h "
            f"{minutes:02d} min"
        )

    return f"{minutes} min"


# =========================================================
# ROUTE OPTIMIZATION
# =========================================================

def optimize_route(
    origin,
    clients,
    round_trip=False,
    avoid_tolls=True,
):

    info = json.loads(
        st.secrets[
            "GCP_SERVICE_ACCOUNT_JSON"
        ]
    )

    project_id = info["project_id"]

    url = (
        "https://routeoptimization."
        "googleapis.com/v1/"
        f"projects/{project_id}:"
        "optimizeTours"
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
                "label":
                    client["original"],

                "deliveries": [
                    {
                        "arrivalWaypoint":
                            waypoint(client),

                        "duration": "0s",

                        "label":
                            client["original"],
                    }
                ],

                "penaltyCost":
                    1000000,
            }
        )

    vehicle = {
        "label": "Viatura 1",

        "startWaypoint":
            waypoint(origin),

        "travelMode":
            "DRIVING",

        "routeModifiers": {
            "avoidTolls":
                avoid_tolls,

            "avoidHighways":
                False,

            "avoidFerries":
                True,
        },

        "costPerTraveledHour":
            1.0,
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
                .replace(
                    "+00:00",
                    "Z",
                ),

            "globalEndTime":
                end_time
                .isoformat()
                .replace(
                    "+00:00",
                    "Z",
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
            "Erro Route Optimization API:"
            f"\n\n{response.status_code}"
            f"\n{response.text}"
        )

    return response.json()


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
        "destination":
            destination,
        "travelmode":
            "driving",
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
        "https://www.google.com/maps/"
        "dir/?"
        + urlencode(
            params,
            safe="|,",
        )
    )


# =========================================================
# PREVIEW LINKS
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
        for client
        in ordered_clients
    )

    if round_trip:
        points.append(
            origin["formatted"]
        )

    links = []

    # Preview:
    # até 9 waypoints intermédios
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
                "number":
                    number,

                "origin":
                    segment[0],

                "destination":
                    segment[-1],

                "url":
                    url,
            }
        )

        start += (
            len(segment) - 1
        )

        number += 1

    return links


# =========================================================
# NAVIGATION LINKS
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
        for client
        in ordered_clients
    ]

    if round_trip:
        points.append(
            return_origin["formatted"]
        )

    links = []

    # Links mobile conservadores:
    # localização atual
    # + 3 waypoints
    # + destino
    clients_per_link = 4

    index = 0
    number = 1

    while index < len(points):

        segment = points[
            index:
            index + clients_per_link
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

                "waypoints":
                    intermediate,

                "url":
                    url,
            }
        )

        index += (
            clients_per_link
        )

        number += 1

    return links


# =========================================================
# CONFIGURAÇÃO DA ROTA
# =========================================================

st.markdown(
    "## Nova rota"
)


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

st.markdown(
    "### 📍 Ponto de partida"
)

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

    selected_origin = None


else:

    st.info(
        "Autoriza a localização "
        "quando o telemóvel pedir."
    )

    location = get_geolocation()

    selected_origin = None

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

            selected_origin = (
                current_location_object(
                    latitude,
                    longitude,
                )
            )

            st.success(
                "✅ Localização obtida"
            )


# =========================================================
# TIPO DE ROTA
# =========================================================

st.markdown(
    "### 🚚 Tipo de rota"
)

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
# PORTAGENS
# =========================================================

st.markdown(
    "### 🛣️ Portagens"
)

toll_mode = st.radio(
    "Preferência",
    [
        "🚫 Evitar",
        "🛣️ Permitir",
    ],
    horizontal=True,
)

avoid_tolls = (
    toll_mode == "🚫 Evitar"
)


# =========================================================
# CLIENTES
# =========================================================

st.markdown(
    "### 📋 Clientes"
)

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


client_count = len(
    [
        line
        for line
        in clients_input.splitlines()
        if line.strip()
    ]
)

st.caption(
    f"{client_count} cliente(s)"
)


# =========================================================
# BOTÃO OTIMIZAR
# =========================================================

if st.button(
    "✨ OTIMIZAR ROTA",
    type="primary",
    use_container_width=True,
):

    client_addresses = [
        line.strip()
        for line
        in clients_input.splitlines()
        if line.strip()
    ]

    if not client_addresses:

        st.error(
            "Adiciona pelo menos "
            "um cliente."
        )

        st.stop()

    try:

        # ---------------------------------------------
        # ORIGEM
        # ---------------------------------------------

        if (
            start_mode
            == "🏢 Base do vendedor"
        ):

            with st.spinner(
                "A localizar "
                "o ponto de partida..."
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
                    "Ainda não temos "
                    "a tua localização. "
                    "Autoriza primeiro "
                    "o acesso à localização."
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


        # ---------------------------------------------
        # CLIENTES
        # ---------------------------------------------

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
                / len(
                    client_addresses
                )
            )

        progress.empty()


        # ---------------------------------------------
        # OTIMIZAR
        # ---------------------------------------------

        with st.spinner(
            "✨ A calcular "
            "a melhor rota..."
        ):

            result = (
                optimize_route(
                    origin,
                    clients,
                    round_trip=round_trip,
                    avoid_tolls=avoid_tolls,
                )
            )


        routes = (
            result.get(
                "routes",
                [],
            )
        )

        if not routes:

            st.error(
                "Não foi encontrada "
                "uma rota."
            )

            st.stop()


        route = routes[0]

        visits = route.get(
            "visits",
            [],
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

            "avoid_tolls":
                avoid_tolls,

            "route":
                route,
        }


        st.session_state.manual_order = [
            client["original"]
            for client
            in ordered_clients
        ]

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

    st.divider()

    st.markdown(
        "## ✨ Rota sugerida"
    )

    st.caption(
        f"👤 {data['seller']}"
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

    total_time = (
        duration_seconds(
            metrics.get(
                "travelDuration",
                "0s",
            )
        )
    )


    # =====================================================
    # MÉTRICAS
    # =====================================================

    col1, col2, col3 = (
        st.columns(3)
    )

    col1.metric(
        "KM",
        f"{total_km:.1f}",
    )

    col2.metric(
        "Tempo",
        format_duration(
            total_time
        ),
    )

    col3.metric(
        "Clientes",
        len(
            data[
                "ordered_clients"
            ]
        ),
    )


    if data["avoid_tolls"]:

        st.caption(
            "🚫 A evitar portagens"
        )

    else:

        st.caption(
            "🛣️ Portagens permitidas"
        )


    # =====================================================
    # PREVIEW ORIGINAL
    # =====================================================

    preview_links = (
        build_preview_links(
            data["origin"],
            data[
                "ordered_clients"
            ],
            data["round_trip"],
            data["avoid_tolls"],
        )
    )


    for link in preview_links:

        button_text = (
            "🗺️ VER ROTA NO GOOGLE MAPS"
        )

        if len(
            preview_links
        ) > 1:

            button_text = (
                "🗺️ VER ROTA "
                f"{link['number']} "
                "NO GOOGLE MAPS"
            )

        st.link_button(
            button_text,
            link["url"],
            use_container_width=True,
        )


    # =====================================================
    # DRAG & DROP
    # =====================================================

    st.divider()

    st.markdown(
        "## ↕️ Ajustar ordem"
    )

    st.caption(
        "Mantém pressionado e arrasta "
        "cada cliente para a posição "
        "pretendida."
    )


    # Criamos IDs para não haver
    # problemas com moradas repetidas

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


    sortable_style = """
    .sortable-component {
        font-size: 15px;
    }

    .sortable-item {
        background: rgba(128,128,128,0.08);
        border: 1px solid rgba(128,128,128,0.25);
        border-radius: 12px;
        margin-bottom: 8px;
        padding: 14px 12px;
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
            for item
            in sorted_display
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


    # =====================================================
    # ORDEM ATUAL
    # =====================================================

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


    st.markdown(
        "### Ordem atual"
    )


    for index, client in enumerate(
        current_clients,
        start=1,
    ):

        st.markdown(
            f"""
            <div class="route-card">
                <div class="route-number">
                    PARAGEM {index}
                </div>
                <div class="route-address">
                    {client["original"]}
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )


    # =====================================================
    # PREVIEW ORDEM ATUAL
    # =====================================================

    adjusted_preview = (
        build_preview_links(
            data["origin"],
            current_clients,
            data["round_trip"],
            data["avoid_tolls"],
        )
    )


    for link in adjusted_preview:

        text = (
            "🗺️ VER ORDEM ATUAL "
            "NO GOOGLE MAPS"
        )

        if len(
            adjusted_preview
        ) > 1:

            text = (
                "🗺️ VER ORDEM ATUAL "
                f"{link['number']}"
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

    if not st.session_state.validated:

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
        "Rota validada. "
        "Os links abaixo estão "
        "prontos para o motorista."
    )


    # =====================================================
    # LINKS DE NAVEGAÇÃO
    # =====================================================

    navigation_links = (
        build_navigation_links(
            final_clients,
            data["round_trip"],
            data["origin"],
            data["avoid_tolls"],
        )
    )


    for link in navigation_links:

        st.markdown(
            f"### 🚚 Parte "
            f"{link['number']}"
        )

        st.caption(
            "A navegação começa "
            "na localização atual "
            "do motorista."
        )

        st.link_button(
            (
                "▶️ INICIAR NAVEGAÇÃO "
                f"— PARTE "
                f"{link['number']}"
            ),
            link["url"],
            use_container_width=True,
            type="primary",
        )


    st.caption(
        f"{len(navigation_links)} "
        "parte(s) de navegação"
    )


    # =====================================================
    # NOVA ROTA
    # =====================================================

    st.divider()

    if st.button(
        "➕ Criar nova rota",
        use_container_width=True,
    ):

        st.session_state.route_data = (
            None
        )

        st.session_state.manual_order = (
            None
        )

        st.session_state.validated = (
            False
        )

        st.rerun()
