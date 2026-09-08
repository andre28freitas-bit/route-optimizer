import json
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode

import requests
import streamlit as st
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import service_account
from streamlit_js_eval import get_geolocation
from streamlit_sortables import sort_items

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
        max-width: 760px;
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

SELLERS = {
    "Vendedor Teste": (
        "Minho Jantes, Rua Da Tomada 13, "
        "4730-325 Oleiros, Vila Verde"
    ),
}

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

    response = requests.get(url, headers=headers, timeout=30)

    if response.status_code != 200:
        raise RuntimeError(
            f"Erro Geocoding API ({response.status_code}): {response.text}"
        )

    results = response.json().get("results", [])
    if not results:
        raise ValueError(f"Não foi possível localizar: {address}")

    result = results[0]
    return {
        "original": address,
        "formatted": result.get("formattedAddress", address),
        "place_id": result.get("placeId"),
        "latitude": result["location"]["latitude"],
        "longitude": result["location"]["longitude"],
    }

def optimize_route(origin, clients, round_trip, avoid_tolls):
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
            f"Erro Route Optimization API ({response.status_code}): {response.text}"
        )

    return response.json()

def compute_fixed_route(
    origin,
    ordered_clients,
    round_trip,
    avoid_tolls,
    emission_type,
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
        "extraComputations": ["TOLLS"],
        "languageCode": "pt-PT",
        "units": "METRIC",
    }

    headers = auth_headers()
    headers["X-Goog-FieldMask"] = (
        "routes.distanceMeters,"
        "routes.duration,"
        "routes.travelAdvisory.tollInfo"
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
    toll_info = route.get("travelAdvisory", {}).get("tollInfo")

    toll_cost = 0.0
    toll_currency = "EUR"
    contains_tolls = bool(toll_info)
    toll_known = True

    if toll_info:
        prices = toll_info.get("estimatedPrice", [])
        if prices:
            toll_cost = sum(money_to_float(price) for price in prices)
            toll_currency = prices[0].get("currencyCode", "EUR")
        else:
            toll_known = False

    return {
        "distance_km": route.get("distanceMeters", 0) / 1000,
        "duration_s": duration_seconds(route.get("duration", "0s")),
        "contains_tolls": contains_tolls,
        "toll_known": toll_known,
        "toll_cost": toll_cost,
        "toll_currency": toll_currency,
        "avoid_tolls": avoid_tolls,
    }

def add_costs(route, consumption, fuel_price, driver_hour_cost):
    fuel_litres = route["distance_km"] * consumption / 100
    fuel_cost = fuel_litres * fuel_price
    driver_cost = route["duration_s"] / 3600 * driver_hour_cost

    route["fuel_litres"] = fuel_litres
    route["fuel_cost"] = fuel_cost
    route["driver_cost"] = driver_cost

    if route["toll_known"]:
        route["total_cost"] = fuel_cost + route["toll_cost"] + driver_cost
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
        origin, ordered_clients, round_trip, False, emission_type
    )
    without_tolls = compute_fixed_route(
        origin, ordered_clients, round_trip, True, emission_type
    )

    with_tolls = add_costs(
        with_tolls, consumption, fuel_price, driver_hour_cost
    )
    without_tolls = add_costs(
        without_tolls, consumption, fuel_price, driver_hour_cost
    )

    return {
        "with_tolls": with_tolls,
        "without_tolls": without_tolls,
    }

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
        + urlencode(params, safe="|,")
    )

def build_preview_links(origin, ordered_clients, round_trip, avoid_tolls):
    if not ordered_clients:
        return []

    points = [origin["formatted"]]
    points.extend(client["formatted"] for client in ordered_clients)

    if round_trip:
        points.append(origin["formatted"])

    links = []
    max_points = 11
    start = 0
    number = 1

    while start < len(points) - 1:
        segment = points[start:start + max_points]

        if len(segment) < 2:
            break

        links.append(
            {
                "number": number,
                "url": google_maps_url(
                    origin=segment[0],
                    waypoints=segment[1:-1],
                    destination=segment[-1],
                    avoid_tolls=avoid_tolls,
                    navigation=False,
                ),
            }
        )

        start += len(segment) - 1
        number += 1

    return links

def build_navigation_links(
    ordered_clients,
    round_trip,
    return_origin,
    avoid_tolls,
):
    if not ordered_clients:
        return []

    points = [client["formatted"] for client in ordered_clients]

    if round_trip:
        points.append(return_origin["formatted"])

    links = []
    points_per_link = 4
    index = 0
    number = 1

    while index < len(points):
        segment = points[index:index + points_per_link]

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

def show_comparison_card(title, route):
    with st.container(border=True):
        st.subheader(title)

        col1, col2 = st.columns(2)
        with col1:
            st.metric("Distância", f"{route['distance_km']:.1f} km")
        with col2:
            st.metric("Tempo", format_duration(route["duration_s"]))

        col3, col4 = st.columns(2)
        with col3:
            st.metric("Combustível", euro(route["fuel_cost"]))
        with col4:
            if route["contains_tolls"]:
                if route["toll_known"]:
                    if route["toll_currency"] == "EUR":
                        toll_text = euro(route["toll_cost"])
                    else:
                        toll_text = (
                            f"{route['toll_cost']:.2f} "
                            f"{route['toll_currency']}"
                        )
                else:
                    toll_text = "Indisponível"
            else:
                toll_text = "0,00 €"

            st.metric("Portagens", toll_text)

        if route["total_cost"] is not None:
            st.metric("Custo estimado total", euro(route["total_cost"]))
        else:
            st.metric("Custo estimado total", "—")

st.title("🚚 Route Optimizer")
st.caption("Planeia. Compara. Otimiza. Navega.")
st.markdown("## Nova rota")

seller = st.selectbox(
    "👤 Vendedor",
    list(SELLERS.keys()),
)
predefined_address = SELLERS[seller]

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
        "Será usada a localização do dispositivo onde a app está aberta."
    )

    location = get_geolocation()

    if location and location.get("coords"):
        latitude = location["coords"].get("latitude")
        longitude = location["coords"].get("longitude")

        if latitude is not None and longitude is not None:
            st.session_state.current_location = {
                "latitude": latitude,
                "longitude": longitude,
            }
            st.success("✅ Localização atual obtida")

st.markdown("### 🚚 Tipo de rota")

mode = st.radio(
    "Percurso",
    [
        "➡️ Rota aberta",
        "🔁 Ida e volta",
    ],
    horizontal=True,
)

round_trip = mode == "🔁 Ida e volta"

st.markdown("### 🛣️ Preferência inicial")

optimization_choice = st.radio(
    "Como queres otimizar inicialmente?",
    [
        "⚡ Portagens permitidas",
        "🚫 Evitar portagens",
    ],
)

optimization_avoid_tolls = (
    optimization_choice == "🚫 Evitar portagens"
)

with st.expander("💶 Custos da viatura"):
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
    emission_label = st.selectbox(
        "Tipo de motor",
        ["Diesel", "Gasolina", "Híbrido", "Elétrico"],
    )

EMISSIONS = {
    "Diesel": "DIESEL",
    "Gasolina": "GASOLINE",
    "Híbrido": "HYBRID",
    "Elétrico": "ELECTRIC",
}
emission_type = EMISSIONS[emission_label]

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

st.caption(f"{len(client_addresses)} cliente(s)")

if st.button(
    "✨ OTIMIZAR E COMPARAR",
    type="primary",
    use_container_width=True,
):
    if not client_addresses:
        st.error("Adiciona pelo menos um cliente.")
        st.stop()

    try:
        if start_mode == "🏢 Base do vendedor":
            with st.spinner("A localizar ponto de partida..."):
                origin = geocode_address(predefined_address)
        else:
            current = st.session_state.current_location

            if not current:
                st.error(
                    "Ainda não foi possível obter a localização atual."
                )
                st.stop()

            origin = current_location_object(
                current["latitude"],
                current["longitude"],
            )

        clients = []
        progress = st.progress(0)

        for index, address in enumerate(client_addresses):
            clients.append(geocode_address(address))
            progress.progress((index + 1) / len(client_addresses))

        progress.empty()

        with st.spinner("✨ A encontrar a melhor ordem de visitas..."):
            result = optimize_route(
                origin,
                clients,
                round_trip,
                optimization_avoid_tolls,
            )

        routes = result.get("routes", [])
        if not routes:
            st.error("Não foi encontrada nenhuma rota.")
            st.stop()

        optimization_route = routes[0]
        ordered_clients = []

        for visit in optimization_route.get("visits", []):
            shipment_index = visit.get("shipmentIndex")

            if shipment_index is not None:
                ordered_clients.append(clients[shipment_index])

        if not ordered_clients:
            st.error("A otimização não devolveu nenhuma visita.")
            st.stop()

        with st.spinner("💶 A comparar com e sem portagens..."):
            comparison = compare_routes(
                origin,
                ordered_clients,
                round_trip,
                consumption,
                fuel_price,
                driver_hour_cost,
                emission_type,
            )

        st.session_state.route_data = {
            "seller": seller,
            "origin": origin,
            "clients": clients,
            "round_trip": round_trip,
            "consumption": consumption,
            "fuel_price": fuel_price,
            "driver_hour_cost": driver_hour_cost,
            "emission_type": emission_type,
        }

        st.session_state.manual_order = [
            client["original"]
            for client in ordered_clients
        ]
        st.session_state.route_comparison = comparison
        st.session_state.comparison_order = (
            st.session_state.manual_order.copy()
        )
        st.session_state.final_avoid_tolls = optimization_avoid_tolls
        st.session_state.validated = False

        st.rerun()

    except Exception as error:
        st.error("Ocorreu um erro.")
        st.code(str(error))

if st.session_state.route_data:
    data = st.session_state.route_data

    client_lookup = {
        client["original"]: client
        for client in data["clients"]
    }

    current_clients = [
        client_lookup[name]
        for name in st.session_state.manual_order
    ]

    st.divider()
    st.markdown("## ✨ Rota otimizada")
    st.caption(
        f"👤 {data['seller']} · {len(current_clients)} clientes"
    )

    comparison = st.session_state.route_comparison

    if comparison:
        st.markdown("## 💶 Comparação de rotas")

        show_comparison_card(
            "🛣️ Portagens permitidas",
            comparison["with_tolls"],
        )

        show_comparison_card(
            "🚫 Evitar portagens",
            comparison["without_tolls"],
        )

        with_tolls = comparison["with_tolls"]
        without_tolls = comparison["without_tolls"]

        time_diff = without_tolls["duration_s"] - with_tolls["duration_s"]
        km_diff = without_tolls["distance_km"] - with_tolls["distance_km"]

        if time_diff > 60:
            st.info(
                "🚫 Sem portagens acrescenta "
                f"{format_duration(time_diff)}."
            )
        elif time_diff < -60:
            st.info(
                "🚫 Sem portagens é "
                f"{format_duration(abs(time_diff))} mais rápido."
            )
        else:
            st.info(
                "⏱️ O tempo das duas alternativas é semelhante."
            )

        if abs(km_diff) >= 0.1:
            sign = "+" if km_diff > 0 else ""
            st.caption(
                f"📏 Diferença sem portagens: {sign}{km_diff:.1f} km"
            )

        if (
            with_tolls["total_cost"] is not None
            and without_tolls["total_cost"] is not None
        ):
            cost_diff = (
                without_tolls["total_cost"]
                - with_tolls["total_cost"]
            )

            if cost_diff < -0.01:
                st.success(
                    "💰 Sem portagens poupa "
                    f"{euro(abs(cost_diff))}."
                )
            elif cost_diff > 0.01:
                st.success(
                    "💰 Com portagens poupa "
                    f"{euro(cost_diff)}."
                )
            else:
                st.success(
                    "💰 O custo estimado é praticamente igual."
                )

    st.markdown("### Qual rota queres usar?")

    choice = st.radio(
        "Opção final",
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
        choice == "🚫 Evitar portagens"
    )

    preview_links = build_preview_links(
        data["origin"],
        current_clients,
        data["round_trip"],
        st.session_state.final_avoid_tolls,
    )

    for link in preview_links:
        label = "🗺️ VER ROTA NO GOOGLE MAPS"

        if len(preview_links) > 1:
            label += f" — PARTE {link['number']}"

        st.link_button(
            label,
            link["url"],
            use_container_width=True,
        )

    st.divider()
    st.markdown("## ↕️ Ajustar visitas")
    st.caption("Mantém pressionado e arrasta para mudar a ordem.")

    drag_items = []
    drag_lookup = {}

    for index, client_name in enumerate(
        st.session_state.manual_order,
        start=1,
    ):
        display = f"{index}. {client_name}"
        drag_items.append(display)
        drag_lookup[display] = client_name

    sorted_display = sort_items(
        drag_items,
        direction="vertical",
    )

    if sorted_display:
        new_order = [
            drag_lookup[item]
            for item in sorted_display
        ]

        if new_order != st.session_state.manual_order:
            st.session_state.manual_order = new_order
            st.session_state.validated = False
            st.rerun()

    comparison_is_current = (
        st.session_state.comparison_order
        == st.session_state.manual_order
    )

    if not comparison_is_current:
        st.warning(
            "Alteraste a ordem. Recalcula os custos antes de validar."
        )

        if st.button(
            "💶 RECALCULAR COMPARAÇÃO",
            use_container_width=True,
        ):
            try:
                current_clients = [
                    client_lookup[name]
                    for name in st.session_state.manual_order
                ]

                with st.spinner("A recalcular..."):
                    comparison = compare_routes(
                        data["origin"],
                        current_clients,
                        data["round_trip"],
                        data["consumption"],
                        data["fuel_price"],
                        data["driver_hour_cost"],
                        data["emission_type"],
                    )

                st.session_state.route_comparison = comparison
                st.session_state.comparison_order = (
                    st.session_state.manual_order.copy()
                )

                st.rerun()

            except Exception as error:
                st.error("Não foi possível recalcular.")
                st.code(str(error))

    current_clients = [
        client_lookup[name]
        for name in st.session_state.manual_order
    ]

    st.markdown("### 📋 Ordem atual")

    for index, client in enumerate(current_clients, start=1):
        with st.container(border=True):
            st.caption(f"PARAGEM {index}")
            st.write(client["original"])

    current_preview = build_preview_links(
        data["origin"],
        current_clients,
        data["round_trip"],
        st.session_state.final_avoid_tolls,
    )

    for link in current_preview:
        label = "🗺️ PRÉ-VISUALIZAR ORDEM ATUAL"

        if len(current_preview) > 1:
            label += f" — PARTE {link['number']}"

        st.link_button(
            label,
            link["url"],
            use_container_width=True,
        )

    st.divider()

    comparison_is_current = (
        st.session_state.comparison_order
        == st.session_state.manual_order
    )

    if comparison_is_current and not st.session_state.validated:
        if st.button(
            "✅ VALIDAR ROTA",
            type="primary",
            use_container_width=True,
        ):
            st.session_state.validated = True
            st.rerun()

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
    st.markdown("## ✅ Rota pronta")

    st.success(
        "Rota validada e pronta para o motorista."
    )

    navigation_links = build_navigation_links(
        final_clients,
        data["round_trip"],
        data["origin"],
        st.session_state.final_avoid_tolls,
    )

    for link in navigation_links:
        st.markdown(f"### 🚚 Parte {link['number']}")

        st.link_button(
            f"▶️ INICIAR NAVEGAÇÃO — PARTE {link['number']}",
            link["url"],
            use_container_width=True,
            type="primary",
        )

    if len(navigation_links) > 1:
        st.caption(
            "Quando terminar uma parte, abre a seguinte."
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
