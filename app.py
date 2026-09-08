
import re
import urllib.parse
import requests
import pandas as pd
import streamlit as st

ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
FIELD_MASK = ",".join([
    "routes.distanceMeters",
    "routes.duration",
    "routes.legs.distanceMeters",
    "routes.legs.duration",
    "routes.optimizedIntermediateWaypointIndex",
])

DEFAULT_ORIGIN = "Rua Da Tomada 13, 4730-325, Portugal"

DEFAULT_STOPS = [
    ("Matriz Auto Braga", "R. Cidade do Porto 62, 4705-084 Braga, Portugal"),
    ("Braga Retail Park", "Braga Retail Park - Loja K, Lugar De Passos E Lameiras, 4710-426 Braga, Portugal"),
    ("Nova Arcada", "Centro Comercial Nova Arcada, Avenida De Lamas 100 Loja R 05.A, 4700-068 Braga, Portugal"),
    ("Vila Verde", "Av. Antonio Sergio 508, 4730-709 Vila Verde, Portugal"),
    ("Minho Center", "C.C. Minho Center 59, Av. Robert Smith - Fraião, 4715-249 Braga, Portugal"),
    ("Centro Empresarial de Braga", "Centro Empresarial de Braga - Largo da Misericordia, Pav W2/W3, 4705-319 Braga, Portugal"),
    ("Porto", "Av. Fontes Pereira de Melo 318, 4100-259 Porto, Portugal"),
    ("Av. da Independência", "Av. da Independência 1 1C, 4705-162 Braga, Portugal"),
    ("Ferreiros", "Travessa Marceliano de Araújo 49, Ferreiros, 4705-101 Braga, Portugal"),
    ("Av. Barros e Soares", "Av. Barros e Soares 130, 4715-214 Braga, Portugal"),
    ("BMcar Braga", "N101, 4715-213 Braga, Portugal"),
]

def seconds_from_duration(value):
    if not value:
        return 0
    m = re.fullmatch(r"([0-9.]+)s", value)
    return float(m.group(1)) if m else 0

def fmt_duration(seconds):
    minutes = round(seconds / 60)
    h, m = divmod(minutes, 60)
    return f"{h}h {m:02d}min" if h else f"{m} min"

def fmt_km(meters):
    return f"{meters / 1000:.1f} km"

def compute_route(api_key, origin, destination, intermediates, avoid_tolls=True):
    body = {
        "origin": {"address": origin},
        "destination": {"address": destination},
        "intermediates": [{"address": x} for x in intermediates],
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE",
        "optimizeWaypointOrder": True,
        "routeModifiers": {
            "avoidTolls": bool(avoid_tolls),
            "avoidHighways": False,
            "avoidFerries": True
        },
        "languageCode": "pt-PT",
        "units": "METRIC"
    }

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": FIELD_MASK,
    }

    r = requests.post(ROUTES_URL, json=body, headers=headers, timeout=30)
    if not r.ok:
        try:
            detail = r.json()
        except Exception:
            detail = r.text
        raise RuntimeError(f"Google Routes API: HTTP {r.status_code}\n{detail}")

    data = r.json()
    if not data.get("routes"):
        raise RuntimeError("A Google não devolveu nenhuma rota.")
    return data["routes"][0]

def optimized_sequence(original_intermediates, route):
    order = route.get("optimizedIntermediateWaypointIndex", list(range(len(original_intermediates))))
    return [original_intermediates[i] for i in order]

def maps_url(origin, destination, waypoints):
    params = {
        "api": "1",
        "origin": origin,
        "destination": destination,
        "travelmode": "driving",
        "avoid": "tolls",
    }
    if waypoints:
        params["waypoints"] = "|".join(waypoints)
    return "https://www.google.com/maps/dir/?" + urllib.parse.urlencode(
        params, safe="|,"
    )

def split_mobile_links(full_sequence, max_intermediate=3):
    # full_sequence contains origin, stops..., final destination.
    # Google docs: mobile browsers may support only 3 waypoints.
    links = []
    start_idx = 0
    while start_idx < len(full_sequence) - 1:
        end_idx = min(start_idx + max_intermediate + 1, len(full_sequence) - 1)
        segment = full_sequence[start_idx:end_idx + 1]
        origin = segment[0]
        destination = segment[-1]
        waypoints = segment[1:-1]
        links.append((origin, destination, waypoints, maps_url(origin, destination, waypoints)))
        start_idx = end_idx
    return links

def route_table(names_and_addresses, route, start_label, start_address, final_label, final_address):
    legs = route.get("legs", [])
    rows = []
    previous_label = start_label
    previous_address = start_address

    ordered = names_and_addresses + [(final_label, final_address)]
    for idx, ((label, address), leg) in enumerate(zip(ordered, legs), start=1):
        rows.append({
            "#": idx,
            "De": previous_label,
            "Para": label,
            "Distância": fmt_km(leg.get("distanceMeters", 0)),
            "Duração": fmt_duration(seconds_from_duration(leg.get("duration"))),
            "Morada": address,
        })
        previous_label, previous_address = label, address
    return pd.DataFrame(rows)

st.set_page_config(page_title="Otimizador de Rotas", page_icon="🚗", layout="wide")
st.title("🚗 Otimizador de Rotas — sem portagens")
st.caption("Google Routes API · ordem automática · km/tempo · links Google Maps")

with st.sidebar:
    st.header("Configuração")
    try:
        api_key = st.secrets["GOOGLE_MAPS_API_KEY"]
        st.success("Google Maps API configurada no servidor.")
    except Exception:
        api_key = st.text_input("Google Maps API key", type="password")
        st.info("Para testes locais podes colar a chave aqui. Online, guarda-a nos Secrets do Streamlit.")
    avoid_tolls = st.checkbox("Evitar portagens", value=True)

origin = st.text_input("Origem", value=DEFAULT_ORIGIN)

default_df = pd.DataFrame(DEFAULT_STOPS, columns=["Cliente", "Morada"])
uploaded = st.file_uploader("Opcional: carregar CSV ou Excel com colunas Cliente, Morada", type=["csv", "xlsx"])

if uploaded:
    if uploaded.name.lower().endswith(".xlsx"):
        df = pd.read_excel(uploaded)
    else:
        df = pd.read_csv(uploaded)
    expected = {"Cliente", "Morada"}
    if not expected.issubset(df.columns):
        st.error("O CSV precisa das colunas: Cliente, Morada")
        st.stop()
    df = df[["Cliente", "Morada"]].dropna()
else:
    df = default_df.copy()

edited = st.data_editor(df, num_rows="dynamic", use_container_width=True)

if st.button("CALCULAR AS DUAS ROTAS", type="primary", use_container_width=True):
    if not api_key:
        st.error("Introduz primeiro a Google Maps API key.")
        st.stop()

    edited = edited.dropna(subset=["Cliente", "Morada"])
    stops = list(edited.itertuples(index=False, name=None))
    if len(stops) < 2:
        st.error("São necessárias pelo menos 2 paragens.")
        st.stop()
    if len(stops) > 25:
        st.error("Este protótipo usa Compute Routes standard: máximo 25 paragens intermédias.")
        st.stop()

    # TESTE A — rota aberta:
    # A API otimiza intermédios, mas o destination é fixo.
    # Testamos cada cliente como destination e escolhemos a menor duração.
    open_candidates = []
    progress = st.progress(0, text="A testar possíveis destinos finais...")
    for i, candidate in enumerate(stops):
        final_name, final_address = candidate
        interm = [x for x in stops if x != candidate]
        interm_addresses = [x[1] for x in interm]
        route = compute_route(api_key, origin, final_address, interm_addresses, avoid_tolls)

        optimized_addresses = optimized_sequence(interm_addresses, route)
        by_address = {address: name for name, address in interm}
        optimized_named = [(by_address[a], a) for a in optimized_addresses]

        open_candidates.append({
            "route": route,
            "optimized_named": optimized_named,
            "final_name": final_name,
            "final_address": final_address,
        })
        progress.progress((i + 1) / len(stops), text=f"Destino final testado: {final_name}")

    best_open = min(open_candidates, key=lambda x: seconds_from_duration(x["route"].get("duration")))
    progress.empty()

    # TESTE B — circuito fechado:
    all_addresses = [x[1] for x in stops]
    closed_route = compute_route(api_key, origin, origin, all_addresses, avoid_tolls)
    closed_opt_addresses = optimized_sequence(all_addresses, closed_route)
    all_by_address = {address: name for name, address in stops}
    closed_named = [(all_by_address[a], a) for a in closed_opt_addresses]

    tab_a, tab_b = st.tabs(["A · Terminar no melhor cliente", "B · Regressar à origem"])

    with tab_a:
        route = best_open["route"]
        ordered_named = best_open["optimized_named"]
        final_name = best_open["final_name"]
        final_address = best_open["final_address"]

        c1, c2, c3 = st.columns(3)
        c1.metric("Distância total", fmt_km(route.get("distanceMeters", 0)))
        c2.metric("Tempo total", fmt_duration(seconds_from_duration(route.get("duration"))))
        c3.metric("Último cliente", final_name)

        table = route_table(
            ordered_named, route,
            "Minho Jantes / Origem", origin,
            final_name, final_address
        )
        st.dataframe(table, use_container_width=True, hide_index=True)

        full_sequence = [origin] + [a for _, a in ordered_named] + [final_address]
        desktop_url = maps_url(origin, final_address, [a for _, a in ordered_named])

        if len(ordered_named) <= 9:
            st.link_button("🗺️ Abrir rota completa no Google Maps", desktop_url, use_container_width=True)
        else:
            st.warning("A rota excede o limite de waypoints de um único Maps URL. Usa os segmentos abaixo.")

        st.caption("Links móveis seguros (máximo 3 paragens intermédias por link):")
        for n, (_, _, _, url) in enumerate(split_mobile_links(full_sequence), start=1):
            st.link_button(f"📱 Abrir segmento {n} no Google Maps", url, use_container_width=True)

    with tab_b:
        route = closed_route
        c1, c2, c3 = st.columns(3)
        c1.metric("Distância total", fmt_km(route.get("distanceMeters", 0)))
        c2.metric("Tempo total", fmt_duration(seconds_from_duration(route.get("duration"))))
        c3.metric("Fim", "Origem")

        table = route_table(
            closed_named, route,
            "Minho Jantes / Origem", origin,
            "Minho Jantes / Origem", origin
        )
        st.dataframe(table, use_container_width=True, hide_index=True)

        full_sequence = [origin] + [a for _, a in closed_named] + [origin]

        # 10 clientes + regresso => 10 intermédios; Maps URL desktop documenta máx. 9.
        if len(closed_named) <= 9:
            desktop_url = maps_url(origin, origin, [a for _, a in closed_named])
            st.link_button("🗺️ Abrir rota completa no Google Maps", desktop_url, use_container_width=True)
        else:
            st.warning(
                "Esta rota tem mais de 9 paragens intermédias. "
                "O Google Maps URL documenta até 9 fora de mobile; por isso a app divide-a automaticamente."
            )

        st.caption("Links móveis/segmentados, mantendo exatamente a ordem otimizada:")
        for n, (_, _, _, url) in enumerate(split_mobile_links(full_sequence), start=1):
            st.link_button(f"📱 Abrir segmento {n} no Google Maps", url, use_container_width=True)
