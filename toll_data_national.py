"""
Base nacional de portagens Portugal — 2026

Objetivo:
- Carregar automaticamente todas as tarifas publicadas pelo IMT na página nacional.
- Aplicar regras de preço zero em vigor para antigas SCUT / troços isentos.
- Normalizar os dados para uso pela app de otimização de rotas.

Nota:
- Algumas concessões (Norte, Grande Lisboa, Grande Porto, Costa de Prata e Norte Litoral)
  são remetidas pelo IMT para a Infraestruturas de Portugal. Essas ficam identificadas
  como fontes a completar/validar com IP quando necessário.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from typing import Iterable, Optional
import requests
from bs4 import BeautifulSoup


IMT_2026_URL = (
    "https://www.imt-ip.pt/rodoviario/infraestruturas-rodoviarias/"
    "rede-rodoviaria/taxas-de-portagem/"
)

IP_TOLLS_URL = "https://servicos.infraestruturasdeportugal.pt/portagens-ips"

YEAR = 2026


@dataclass(frozen=True)
class TollSegment:
    road: str
    segment_no: str
    start: str
    end: str
    description: str
    km: float
    class1: float
    class2: float
    class3: float
    class4: float
    operator: str = ""
    source: str = "IMT"
    year: int = YEAR

    def price(self, vehicle_class: int = 1) -> float:
        if vehicle_class not in (1, 2, 3, 4):
            raise ValueError("vehicle_class tem de ser 1, 2, 3 ou 4")
        return float(getattr(self, f"class{vehicle_class}"))


# Estradas/troços cujo valor está legalmente fixado em €0 em 2026.
# A regra é aplicada por cima de qualquer tarifa histórica.
ZERO_TOLL_RULES_2026 = [
    {
        "road": "A4",
        "scope": "Transmontana e Túnel do Marão",
        "price": 0.0,
        "source": "Lei n.º 37/2024 + atualização 2026",
    },
    {
        "road": "A13",
        "scope": "Pinhal Interior",
        "price": 0.0,
        "source": "Lei n.º 37/2024",
    },
    {
        "road": "A13-1",
        "scope": "Pinhal Interior",
        "price": 0.0,
        "source": "Lei n.º 37/2024",
    },
    {
        "road": "A22",
        "scope": "Toda a concessão Algarve",
        "price": 0.0,
        "source": "Lei n.º 37/2024",
    },
    {
        "road": "A23",
        "scope": "Beira Interior + Torres Novas/Abrantes abrangido",
        "price": 0.0,
        "source": "Lei n.º 37/2024",
    },
    {
        "road": "A24",
        "scope": "Toda a concessão Interior Norte",
        "price": 0.0,
        "source": "Lei n.º 37/2024",
    },
    {
        "road": "A25",
        "scope": "Toda a extensão em 2026",
        "price": 0.0,
        "source": "Lei n.º 37/2024, alterada pela Lei n.º 73-A/2025",
    },
    {
        "road": "A28",
        "scope": "Esposende–Antas e Neiva–Darque",
        "price": 0.0,
        "source": "Lei n.º 37/2024",
    },
]


# Concessões que a própria página do IMT remete para a IP.
IP_DELEGATED_CONCESSIONS = [
    "Norte",
    "Grande Lisboa",
    "Grande Porto",
    "Costa de Prata",
    "Norte Litoral",
]


def _to_float_pt(value: str) -> float:
    value = (value or "").strip().replace("\xa0", " ")
    value = re.sub(r"[^\d,.\-]", "", value)
    if not value:
        return 0.0
    # Formato PT: 1,15
    if "," in value and "." not in value:
        value = value.replace(",", ".")
    elif "," in value and "." in value:
        value = value.replace(".", "").replace(",", ".")
    try:
        return float(value)
    except ValueError:
        return 0.0


def _norm_road(text: str) -> str:
    text = (text or "").upper()
    text = text.replace("AUTO ESTRADA", "").replace("AUTO-ESTRADA", "")
    text = re.sub(r"\s+", "", text)

    # Primeiro tenta Axx-y / Axx
    m = re.search(r"\bA(\d{1,2})(?:[-/](\d))?", text)
    if m:
        if m.group(2):
            return f"A{m.group(1)}-{m.group(2)}"
        return f"A{m.group(1)}"

    # Casos como A17/IC1
    m = re.search(r"A(\d{1,2})", text)
    if m:
        return f"A{m.group(1)}"

    return text.strip()


def _split_segment(description: str) -> tuple[str, str]:
    # Os dados oficiais usam /, –, — e -.
    parts = re.split(r"\s*(?:/|–|—|\s-\s)\s*", description.strip(), maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return description.strip(), ""


def load_imt_2026(timeout: int = 20) -> list[TollSegment]:
    """
    Faz download da página oficial do IMT e extrai todas as linhas de tarifa
    que estejam publicadas em tabela HTML.

    Retorna segmentos normalizados.
    """
    response = requests.get(
        IMT_2026_URL,
        timeout=timeout,
        headers={"User-Agent": "RouteOptimizer/1.0"},
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    segments: list[TollSegment] = []

    current_road = ""
    current_operator = ""

    # Percorremos tabelas; a página nacional contém uma tabela por autoestrada.
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue

        # Tenta inferir estrada e operador usando texto próximo da tabela.
        prev_text = []
        node = table
        for _ in range(5):
            node = node.find_previous()
            if not node:
                break
            txt = " ".join(node.stripped_strings)
            if txt:
                prev_text.append(txt)

        context = " | ".join(prev_text)
        road_match = re.search(r"\bA\s*([0-9]{1,2})(?:\s*[/\-]\s*([0-9]))?", context, re.I)
        if road_match:
            current_road = f"A{road_match.group(1)}"
            if road_match.group(2):
                current_road += f"-{road_match.group(2)}"

        for op in (
            "Brisa", "Oeste", "Brisal", "Douro Litoral", "Lusoponte",
            "Infraestruturas de Portugal"
        ):
            if op.lower() in context.lower():
                current_operator = op
                break

        for row in rows:
            cells = [" ".join(c.stripped_strings) for c in row.find_all(["td", "th"])]
            if len(cells) < 6:
                continue

            joined = " | ".join(cells)
            if "Total" in joined or "Extensão" in joined or "Taxas de Portagem" in joined:
                continue

            # Localiza quatro valores monetários no fim da linha.
            numeric_positions = []
            for i, cell in enumerate(cells):
                cleaned = re.sub(r"[^\d,.\-]", "", cell)
                if cleaned and re.search(r"\d", cleaned):
                    numeric_positions.append(i)

            if len(numeric_positions) < 5:
                continue

            # Em tabelas típicas os últimos 5 numéricos são km + C1..C4.
            last5 = numeric_positions[-5:]
            km_i, c1_i, c2_i, c3_i, c4_i = last5

            km = _to_float_pt(cells[km_i])
            c1 = _to_float_pt(cells[c1_i])
            c2 = _to_float_pt(cells[c2_i])
            c3 = _to_float_pt(cells[c3_i])
            c4 = _to_float_pt(cells[c4_i])

            if km <= 0 and max(c1, c2, c3, c4) <= 0:
                continue

            # Descrição é normalmente a célula imediatamente antes dos km.
            description = cells[km_i - 1].strip() if km_i > 0 else ""
            if not description or description.lower() in {"lanço", "lanco"}:
                continue

            start, end = _split_segment(description)

            # Estrada pode estar na primeira célula da própria linha.
            row_road = ""
            for cell in cells[:max(1, km_i - 1)]:
                candidate = _norm_road(cell)
                if re.fullmatch(r"A\d{1,2}(?:-\d)?", candidate):
                    row_road = candidate
                    break

            road = row_road or current_road
            if not road:
                continue

            # Número do lanço costuma estar entre estrada e descrição.
            seg_no = ""
            for cell in cells[:km_i - 1]:
                if re.fullmatch(r"\d+[A-Za-z]?", cell.strip()):
                    seg_no = cell.strip()
                    break

            segments.append(
                TollSegment(
                    road=road,
                    segment_no=seg_no,
                    start=start,
                    end=end,
                    description=description,
                    km=km,
                    class1=c1,
                    class2=c2,
                    class3=c3,
                    class4=c4,
                    operator=current_operator,
                )
            )

    return dedupe_segments(segments)


def dedupe_segments(segments: Iterable[TollSegment]) -> list[TollSegment]:
    seen = {}
    for s in segments:
        key = (
            s.road,
            s.description.casefold(),
            round(s.km, 3),
            s.class1,
            s.class2,
            s.class3,
            s.class4,
        )
        seen[key] = s
    return list(seen.values())


def is_zero_toll_corridor(road: str, description: str = "") -> bool:
    """
    Aplica apenas regras inequívocas.
    A28 é parcial, por isso exige match textual do troço.
    """
    road = _norm_road(road)
    desc = (description or "").casefold()

    if road in {"A13", "A13-1", "A22", "A23", "A24", "A25"}:
        return True

    # A4: só parte oriental/Transmontana + Marão; A4 Porto-Amarante continua portajada.
    if road == "A4":
        keywords = (
            "marão", "marao", "vila real", "bragança", "braganca",
            "quintanilha", "transmontana"
        )
        return any(k in desc for k in keywords)

    if road == "A28":
        free_pair_keywords = (
            ("esposende", "antas"),
            ("neiva", "darque"),
        )
        return any(a in desc and b in desc for a, b in free_pair_keywords)

    return False


def apply_2026_zero_overrides(segments: Iterable[TollSegment]) -> list[TollSegment]:
    out = []
    for s in segments:
        if is_zero_toll_corridor(s.road, s.description):
            out.append(
                TollSegment(
                    **{
                        **asdict(s),
                        "class1": 0.0,
                        "class2": 0.0,
                        "class3": 0.0,
                        "class4": 0.0,
                        "source": f"{s.source} + isenção legal 2026",
                    }
                )
            )
        else:
            out.append(s)
    return out


def index_by_road(segments: Iterable[TollSegment]) -> dict[str, list[TollSegment]]:
    result: dict[str, list[TollSegment]] = {}
    for s in segments:
        result.setdefault(s.road, []).append(s)
    return result


def calculate_sequence(
    road: str,
    segment_descriptions: Iterable[str],
    vehicle_class: int = 1,
    segments: Optional[Iterable[TollSegment]] = None,
) -> float:
    """
    Soma segmentos conhecidos por descrição exata/normalizada.
    Serve para validação e para o motor que mais tarde fará matching
    entre a rota Google e os lanços da base.
    """
    if segments is None:
        segments = apply_2026_zero_overrides(load_imt_2026())

    road = _norm_road(road)
    wanted = {re.sub(r"\s+", " ", x.strip()).casefold() for x in segment_descriptions}
    total = 0.0

    for s in segments:
        if s.road != road:
            continue
        key = re.sub(r"\s+", " ", s.description.strip()).casefold()
        if key in wanted:
            total += s.price(vehicle_class)

    return round(total + 1e-9, 2)


def coverage_summary(segments: Iterable[TollSegment]) -> dict:
    segments = list(segments)
    roads = sorted({s.road for s in segments})
    return {
        "year": YEAR,
        "segments_loaded": len(segments),
        "roads_loaded": roads,
        "roads_count": len(roads),
        "official_zero_rules": ZERO_TOLL_RULES_2026,
        "ip_delegated_concessions": IP_DELEGATED_CONCESSIONS,
        "imt_source": IMT_2026_URL,
        "ip_source": IP_TOLLS_URL,
    }


if __name__ == "__main__":
    data = apply_2026_zero_overrides(load_imt_2026())
    info = coverage_summary(data)
    print(f"Segmentos carregados: {info['segments_loaded']}")
    print("Autoestradas:", ", ".join(info["roads_loaded"]))
