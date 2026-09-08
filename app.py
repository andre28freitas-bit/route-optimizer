import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

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
st.caption("Planeamento e navegação de rotas com Google Maps")


# =========================================================
# SESSION STATE
# =========================================================

if "route_data" not in st.session_state:
    st.session_state.route_data = None

if "navigation_active" not in st.session_state:
    st.session_state.navigation_active = False

if "nav_index" not in st.session_state:
    st.session_state.nav_index = 0


# =========================================================
# AUTENTICAÇÃO GOOGLE
# =========================================================

@st.cache_resource
def get_credentials():
    info = json.loads(
        st.secrets["GCP_SERVICE_ACCOUNT_JSON"]
    )

    credentials = (
        service_account.Credentials.from_service_account_info(
            info,
            scopes=[
                "https://www.googleapis.com/auth/cloud-platform"
            ],
        )
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

    from urllib.parse import quote

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
# ROUTE OPTIMIZATION
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


def optimize_route(
    origin,
    clients,
    round_trip=False,
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
            "avoidTolls": True,
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
                start_time
                .isoformat()
                .replace("+00:00", "Z")
            ),
            "globalEndTime": (
                end_time
                .isoformat()
                .replace("+00:00", "Z")
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
            "Erro Route Optimization API:\n\n"
            f"{response.status_code}\n"
            f"{response.text}"
        )

    return response.json()


# =========================================================
# HELPERS
# =========================================================

def duration_seconds(value):
    if not value:
        return 0

    if (
        isinstance(value, str)
        and value.endswith("s")
    ):
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


def navigation_url(location):
    """
    Abre o Google Maps usando a localização atual
    do motorista como origem.
    """

    params = {
        "api": "1",
        "destination": location["formatted"],
        "travelmode": "driving",
        "avoid": "tolls",
        "dir_action": "navigate",
    }

    if location.get("place_id"):
        params["destination_place_id"] = (
            location["place_id"]
        )

    return (
        "https://www.google.com/maps/dir/?"
        + urlencode(params)
    )


def full_route_url(
    origin,
    ordered_clients,
    round_trip,
):
    if not ordered_clients:
        return None

    if round_trip:
        destination = origin
        waypoints = ordered_clients
    else:
        destination = ordered_clients[-1]
        waypoints = ordered_clients[:-1]

    # Google Maps URLs têm limite de waypoints.
    if len(waypoints) > 9:
        return None

    params = {
        "api": "1",
        "origin": origin["formatted"],
        "destination": destination["formatted"],
        "travelmode": "driving",
        "avoid": "tolls",
    }

    if waypoints:
        params["waypoints"] = "|".join(
            item["formatted"]
            for item in waypoints
        )

    return (
        "https://www.google.com/maps/dir/?"
        + urlencode(params)
    )


# =========================================================
# MODO NAVEGAÇÃO
# =========================================================

def render_navigation():
    data = st.session_state.route_data

    if not data:
        return

    ordered_clients = data["ordered_clients"]
    origin = data["origin"]
    round_trip = data["round_trip"]
    transitions = data["transitions"]

    targets = list(ordered_clients)

    if round_trip:
        targets.append(
            {
                **origin,
                "original": "Regresso à origem",
                "is_return": True,
            }
        )

    total_targets = len(targets)
    current_index = st.session_state.nav_index

    st.divider()
    st.markdown("# 🧭 Navegação")

    if current_index >= total_targets:
        st.success(
            "🏁 Rota concluída. Todos os destinos foram visitados."
        )

        if st.button(
            "↩️ Voltar ao planeamento",
            use_container_width=True,
        ):
            st.session_state.navigation_active = False
            st.session_state.nav_index = 0
            st.rerun()

        return

    target = targets[current_index]

    is_return = target.get(
        "is_return",
        False,
    )

    if is_return:
        title = "Regresso à origem"
    else:
        title = target["original"]

    progress_value = (
        current_index / total_targets
        if total_targets
        else 0
    )

    st.progress(progress_value)

    if is_return:
        st.caption("Último percurso")
    else:
        st.caption(
            f"Cliente {current_index + 1} "
            f"de {len(ordered_clients)}"
        )

    st.markdown(f"## 📍 {title}")

    st.write(target["formatted"])

    # Informação prevista para este troço
    if current_index < len(transitions):
        transition = transitions[current_index]

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

        col1, col2 = st.columns(2)

        col1.metric(
            "Distância prevista",
            f"{km:.1f} km",
        )

        col2.metric(
            "Tempo previsto",
            format_duration(seconds),
        )

    maps_url = navigation_url(target)

    st.link_button(
        "🧭 ABRIR NO GOOGLE MAPS",
        maps_url,
        use_container_width=True,
        type="primary",
    )

    st.caption(
        "O Google Maps utiliza a tua localização atual "
        "e inicia a navegação para este destino."
    )

    st.write("")

    if is_return:
        if st.button(
            "🏁 Cheguei à origem — terminar rota",
            use_container_width=True,
        ):
            st.session_state.nav_index += 1
            st.rerun()

    else:
        if st.button(
            "✅ Cliente concluído — próximo",
            use_container_width=True,
            type="primary",
        ):
            st.session_state.nav_index += 1
            st.rerun()

    if current_index > 0:
        if st.button(
            "⬅️ Voltar ao destino anterior",
            use_container_width=True,
        ):
            st.session_state.nav_index -= 1
            st.rerun()

    if st.button(
        "❌ Sair do modo navegação",
        use_container_width=True,
    ):
        st.session_state.navigation_active = False
        st.rerun()


# =========================================================
# SE NAVEGAÇÃO ESTIVER ATIVA
# =========================================================

if st.session_state.navigation_active:
    render_navigation()
    st.stop()


# =========================================================
# INTERFACE DE PLANEAMENTO
# =========================================================

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


# =========================================================
# NOVA OTIMIZAÇÃO
# =========================================================

if optimize_button:
    client_addresses = [
        line.strip()
        for line in clients_input.splitlines()
        if line.strip()
    ]

    if not origin_input.strip():
        st.error(
            "Indica o ponto de partida."
        )
        st.stop()

    if not client_addresses:
        st.error(
            "Adiciona pelo menos um cliente."
        )
        st.stop()

    try:
        with st.spinner(
            "A localizar o ponto de partida..."
        ):
            origin = geocode_address(
                origin_input
            )

        geocoded_clients = []

        progress = st.progress(0)

        for i, address in enumerate(
            client_addresses
        ):
            with st.spinner(
                f"A localizar cliente "
                f"{i + 1}/{len(client_addresses)}..."
            ):
                client = geocode_address(
                    address
                )

                geocoded_clients.append(
                    client
                )

            progress.progress(
                (i + 1)
                / len(client_addresses)
            )

        progress.empty()

        round_trip = mode.startswith(
            "Ida e volta"
        )

        with st.spinner(
            "A calcular a melhor sequência "
            "sem portagens..."
        ):
            result = optimize_route(
                origin,
                geocoded_clients,
                round_trip=round_trip,
            )

        routes = result.get(
            "routes",
            [],
        )

        if not routes:
            st.error(
                "A Google não devolveu "
                "nenhuma rota válida."
            )
            st.stop()

        route = routes[0]

        visits = route.get(
            "visits",
            [],
        )

        transitions = route.get(
            "transitions",
            [],
        )

        ordered_clients = []

        for visit in visits:
            shipment_index = visit.get(
                "shipmentIndex"
            )

            if shipment_index is None:
                continue

            ordered_clients.append(
                geocoded_clients[
                    shipment_index
                ]
            )

        if not ordered_clients:
            st.error(
                "Não foi possível determinar "
                "a ordem das visitas."
            )
            st.stop()

        st.session_state.route_data = {
            "origin": origin,
            "ordered_clients": ordered_clients,
            "round_trip": round_trip,
            "route": route,
            "transitions": transitions,
            "result": result,
        }

        st.session_state.nav_index = 0
        st.session_state.navigation_active = False

    except Exception as error:
        st.error(
            "Ocorreu um erro."
        )
        st.code(
            str(error)
        )


# =========================================================
# MOSTRAR ROTA GUARDADA
# =========================================================

if st.session_state.route_data:
    data = st.session_state.route_data

    origin = data["origin"]
    ordered_clients = data[
        "ordered_clients"
    ]
    round_trip = data["round_trip"]
    route = data["route"]
    transitions = data[
        "transitions"
    ]

    metrics = route.get(
        "metrics",
        {},
    )

    total_distance = metrics.get(
        "travelDistanceMeters",
        0,
    )

    total_seconds = duration_seconds(
        metrics.get(
            "travelDuration",
            "0s",
        )
    )

    st.success(
        "✅ Rota otimizada"
    )

    col1, col2, col3 = st.columns(3)

    col1.metric(
        "Distância total",
        f"{total_distance / 1000:.1f} km",
    )

    col2.metric(
        "Tempo em viagem",
        format_duration(
            total_seconds
        ),
    )

    col3.metric(
        "Clientes",
        len(ordered_clients),
    )

    st.caption(
        "🚫 Sem portagens · "
        "Autoestradas gratuitas permitidas"
    )

    # =====================================================
    # BOTÃO NAVEGAÇÃO
    # =====================================================

    st.markdown("## 🧭 Pronto para sair?")

    if st.button(
        "🧭 INICIAR NAVEGAÇÃO",
        type="primary",
        use_container_width=True,
    ):
        st.session_state.nav_index = 0
        st.session_state.navigation_active = True
        st.rerun()

    # =====================================================
    # ROTA COMPLETA
    # =====================================================

    full_url = full_route_url(
        origin,
        ordered_clients,
        round_trip,
    )

    if full_url:
        st.link_button(
            "🗺️ Ver rota completa no Google Maps",
            full_url,
            use_container_width=True,
        )
    else:
        st.info(
            "A rota tem demasiadas paragens para "
            "um único link do Google Maps. "
            "Usa o modo Navegação, que funciona "
            "cliente a cliente."
        )

    # =====================================================
    # ORDEM OTIMIZADA
    # =====================================================

    st.markdown(
        "## 📍 Ordem otimizada"
    )

    rows = []

    for i, client in enumerate(
        ordered_clients
    ):
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
                "Cliente": client[
                    "original"
                ],
                "Morada": client[
                    "formatted"
                ],
                "Km": f"{km:.1f} km",
                "Tempo": format_duration(
                    seconds
                ),
            }
        )

    if round_trip:
        return_transition = {}

        if len(transitions) > len(
            ordered_clients
        ):
            return_transition = (
                transitions[
                    len(ordered_clients)
                ]
            )

        rows.append(
            {
                "Ordem": "↩",
                "Cliente": (
                    "Regresso à origem"
                ),
                "Morada": origin[
                    "formatted"
                ],
                "Km": (
                    f"{return_transition.get('travelDistanceMeters', 0) / 1000:.1f} km"
                ),
                "Tempo": format_duration(
                    duration_seconds(
                        return_transition.get(
                            "travelDuration",
                            "0s",
                        )
                    )
                ),
            }
        )

    dataframe = pd.DataFrame(
        rows
    )

    st.dataframe(
        dataframe,
        use_container_width=True,
        hide_index=True,
    )

    # =====================================================
    # MORADAS RECONHECIDAS
    # =====================================================

    with st.expander(
        "Ver moradas reconhecidas pela Google"
    ):
        st.write(
            "**Origem:**",
            origin["formatted"],
        )

        for i, client in enumerate(
            ordered_clients,
            start=1,
        ):
            st.write(
                f"**{i}.** "
                f"{client['formatted']}"
            )

    skipped = data["result"].get(
        "skippedShipments",
        [],
    )

    if skipped:
        st.warning(
            f"A Google não conseguiu incluir "
            f"{len(skipped)} cliente(s)."
        )
