import json
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode

import pandas as pd
import requests
import streamlit as st
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import service_account


st.set_page_config(
    page_title="Route Optimizer",
    page_icon="🚚",
    layout="wide",
)

st.title("🚚 Route Optimizer")
st.caption("Otimização de rotas com Google Maps Route Optimization API")


# ---------------------------------------------------------
# AUTENTICAÇÃO GOOGLE
# ---------------------------------------------------------

@st.cache_resource
def get_credentials():
    info = json.loads(st.secrets["GCP_SERVICE_ACCOUNT_JSON"])

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


# ---------------------------------------------------------
# GEOCODING
# ---------------------------------------------------------

@st.cache_data(show_spinner=False)
def geocode_address(address):
    address = address.strip()

    if not address:
        raise ValueError("Morada vazia.")

    encoded_address = quote(address, safe="")

    url = (
        f"https://geocode.googleapis.com/v4/geocode/address/"
        f"{encoded_address}?regionCode=pt&languageCode=pt"
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
            f"Não foi possível localizar a morada: {address}"
        )

    result = results[0]

    return {
        "original": address,
        "formatted": result.get("formattedAddress", address),
        "place_id": result.get("placeId"),
        "latitude": result["location"]["latitude"],
        "longitude": result["location"]["longitude"],
    }


# ---------------------------------------------------------
# ROUTE OPTIMIZATION
# ---------------------------------------------------------

def waypoint(location):
    return {
        "location": {
            "latLng": {
                "latitude": location["latitude"],
                "longitude": location["longitude"],
            }
        }
    }


def optimize_route(origin, clients, round_trip=False):
    project_id = json.loads(
        st.secrets["GCP_SERVICE_ACCOUNT_JSON"]
    )["project_id"]

    url = (
        "https://routeoptimization.googleapis.com/v1/projects/"
        f"{project_id}:optimizeTours"
    )

    now = datetime.now(timezone.utc)
    start_time = now.replace(microsecond=0)
    end_time = start_time + timedelta(hours=24)

    shipments = []

    for index, client in enumerate(clients):
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
                # Torna a visita obrigatória.
                # Um custo muito elevado evita que o solver
                # decida saltar o cliente para poupar percurso.
                "penaltyCost": 1000000,
            }
        )

    vehicle = {
        "label": "Viatura 1",
        "startWaypoint": waypoint(origin),
        "travelMode": "DRIVING",
        "routeModifiers": {
            "avoidTolls": True,
            "avoidHighways": False,
            "avoidFerries": True,
        },
        # Otimizar principalmente o tempo efetivamente em viagem
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
            f"Erro Route Optimization API: "
            f"{response.status_code}\n\n{response.text}"
        )

    return response.json()


# ---------------------------------------------------------
# HELPERS
# ---------------------------------------------------------

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


def google_maps_url(origin, stops, destination):
    params = {
        "api": "1",
        "origin": origin,
        "destination": destination,
        "travelmode": "driving",
        "avoid": "tolls",
    }

    if stops:
        params["waypoints"] = "|".join(stops)

    return "https://www.google.com/maps/dir/?" + urlencode(
        params,
        safe="|,",
    )


def build_maps_links(origin, ordered_addresses, round_trip):
    """
    Google Maps URLs suportam um número limitado de waypoints.
    Se a rota ultrapassar esse limite, divide automaticamente
    em vários links consecutivos.
    """

    if not ordered_addresses:
        return []

    final_destination = (
        origin if round_trip else ordered_addresses[-1]
    )

    intermediates = (
        ordered_addresses
        if round_trip
        else ordered_addresses[:-1]
    )

    # Uma rota completa quando cabe num URL
    if len(intermediates) <= 9:
        return [
            (
                "Abrir rota completa no Google Maps",
                google_maps_url(
                    origin,
                    intermediates,
                    final_destination,
                ),
            )
        ]

    # Caso ultrapasse o limite, dividir em blocos
    all_points = [origin] + ordered_addresses

    if round_trip:
        all_points.append(origin)

    links = []
    current_index = 0
    segment = 1

    # origin + até 9 intermediates + destination
    max_next_points = 10

    while current_index < len(all_points) - 1:
        segment_points = all_points[
            current_index:
            current_index + max_next_points + 1
        ]

        if len(segment_points) < 2:
            break

        segment_origin = segment_points[0]
        segment_destination = segment_points[-1]
        segment_waypoints = segment_points[1:-1]

        links.append(
            (
                f"Abrir segmento {segment} no Google Maps",
                google_maps_url(
                    segment_origin,
                    segment_waypoints,
                    segment_destination,
                ),
            )
        )

        current_index += len(segment_points) - 1
        segment += 1

    return links


# ---------------------------------------------------------
# INTERFACE
# ---------------------------------------------------------

origin_input = st.text_input(
    "📍 Ponto de partida",
    value=(
        "Minho Jantes, Rua Da Tomada 13, "
        "4730-325 Oleiros, Vila Verde"
    ),
)

mode = st.radio(
    "Tipo de rota",
    [
        "Rota aberta — terminar no último cliente",
        "Ida e volta — regressar à origem",
    ],
    horizontal=True,
)

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

optimize_button = st.button(
    "🚀 Otimizar rota",
    type="primary",
    use_container_width=True,
)


# ---------------------------------------------------------
# EXECUÇÃO
# ---------------------------------------------------------

if optimize_button:
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
        with st.spinner("A localizar o ponto de partida..."):
            origin = geocode_address(origin_input)

        geocoded_clients = []

        progress = st.progress(0)

        for i, address in enumerate(client_addresses):
            with st.spinner(
                f"A localizar cliente {i + 1}/{len(client_addresses)}..."
            ):
                geocoded_clients.append(
                    geocode_address(address)
                )

            progress.progress(
                (i + 1) / len(client_addresses)
            )

        progress.empty()

        round_trip = mode.startswith("Ida e volta")

        with st.spinner(
            "A calcular a sequência ótima sem portagens..."
        ):
            result = optimize_route(
                origin,
                geocoded_clients,
                round_trip=round_trip,
            )

        routes = result.get("routes", [])

        if not routes:
            st.error(
                "A Google não devolveu nenhuma rota válida."
            )
            st.stop()

        route = routes[0]
        visits = route.get("visits", [])
        transitions = route.get("transitions", [])

        ordered_clients = []

        for visit in visits:
            shipment_index = visit.get("shipmentIndex")

            if shipment_index is None:
                continue

            ordered_clients.append(
                geocoded_clients[shipment_index]
            )

        if not ordered_clients:
            st.error(
                "Não foi possível determinar a ordem das visitas."
            )
            st.stop()

        # -------------------------------------------------
        # RESULTADOS GERAIS
        # -------------------------------------------------

        metrics = route.get("metrics", {})

        total_distance = metrics.get(
            "travelDistanceMeters",
            0,
        )

        total_seconds = duration_seconds(
            metrics.get("travelDuration", "0s")
        )

        st.success("✅ Rota otimizada")

        col1, col2, col3 = st.columns(3)

        col1.metric(
            "Distância total",
            f"{total_distance / 1000:.1f} km",
        )

        col2.metric(
            "Tempo em viagem",
            format_duration(total_seconds),
        )

        col3.metric(
            "Clientes",
            len(ordered_clients),
        )

        st.caption(
            "🚫 Portagens evitadas · Autoestradas gratuitas permitidas"
        )

        # -------------------------------------------------
        # ORDEM DAS VISITAS
        # -------------------------------------------------

        st.markdown("## 📍 Ordem otimizada")

        rows = []

        previous_name = origin["formatted"]

        for i, client in enumerate(ordered_clients):
            transition = (
                transitions[i]
                if i < len(transitions)
                else {}
            )

            km = (
                transition.get(
                    "travelDistanceMeters",
                    0,
                )
                / 1000
            )

            seconds = duration_seconds(
                transition.get(
                    "travelDuration",
                    "0s",
                )
            )

            rows.append(
                {
                    "Ordem": i + 1,
                    "Cliente": client["original"],
                    "Morada localizada": client["formatted"],
                    "Distância desde anterior": f"{km:.1f} km",
                    "Tempo desde anterior": format_duration(seconds),
                }
            )

            previous_name = client["formatted"]

        if round_trip and len(transitions) > len(visits):
            return_transition = transitions[len(visits)]

            rows.append(
                {
                    "Ordem": "↩",
                    "Cliente": "Regresso à origem",
                    "Morada localizada": origin["formatted"],
                    "Distância desde anterior": (
                        f"{return_transition.get('travelDistanceMeters', 0) / 1000:.1f} km"
                    ),
                    "Tempo desde anterior": format_duration(
                        duration_seconds(
                            return_transition.get(
                                "travelDuration",
                                "0s",
                            )
                        )
                    ),
                }
            )

        dataframe = pd.DataFrame(rows)

        st.dataframe(
            dataframe,
            use_container_width=True,
            hide_index=True,
        )

        # -------------------------------------------------
        # GOOGLE MAPS
        # -------------------------------------------------

        st.markdown("## 🗺️ Navegação")

        ordered_addresses = [
            client["formatted"]
            for client in ordered_clients
        ]

        links = build_maps_links(
            origin["formatted"],
            ordered_addresses,
            round_trip,
        )

        for label, url in links:
            st.link_button(
                f"🗺️ {label}",
                url,
                use_container_width=True,
            )

        # -------------------------------------------------
        # DETALHES DE GEOCODING
        # -------------------------------------------------

        with st.expander("Ver moradas reconhecidas pela Google"):
            st.write(
                "**Origem:**",
                origin["formatted"],
            )

            for i, client in enumerate(
                ordered_clients,
                start=1,
            ):
                st.write(
                    f"**{i}.** {client['formatted']}"
                )

        skipped = result.get("skippedShipments", [])

        if skipped:
            st.warning(
                f"A Google não conseguiu incluir "
                f"{len(skipped)} cliente(s) na rota."
            )

    except Exception as error:
        st.error("Ocorreu um erro.")
        st.code(str(error))
