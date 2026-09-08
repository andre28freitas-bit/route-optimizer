
# Otimizador de Rotas — protótipo

Protótipo Streamlit para:
- receber origem + lista de clientes/moradas;
- evitar portagens;
- otimizar a ordem das visitas;
- mostrar km e tempo total;
- mostrar km/tempo por etapa;
- comparar:
  1. terminar no cliente que produz a rota global mais rápida;
  2. regressar à origem;
- gerar links para abrir a sequência no Google Maps.

## 1. Criar a API key

No Google Cloud Console:
1. Criar/selecionar um projeto.
2. Ativar billing no projeto.
3. Ativar **Routes API**.
4. Criar uma API key.
5. Restringir a key à **Routes API** e, para produção, restringir também por aplicação/IP conforme a arquitetura final.

Este protótipo usa endereços diretamente no Routes API, portanto não precisa obrigatoriamente da Geocoding API.

## 2. Instalar

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

No Windows:

```bash
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## 3. Executar

```bash
streamlit run app.py
```

Abre o endereço local mostrado pelo Streamlit e cola a API key na barra lateral.

## CSV

Formato:

```csv
Cliente,Morada
Cliente A,"Rua X, Braga"
Cliente B,"Av. Y, Vila Verde"
```

## Nota importante sobre Google Maps

A otimização é feita pela Routes API, não pelo link do Google Maps.

O link do Maps recebe a ordem já calculada. A documentação dos Maps URLs indica até 9 waypoints fora de mobile e até 3 em mobile browsers. Por isso o protótipo:
- mostra um link único quando cabe;
- cria automaticamente segmentos mobile-safe quando a rota é maior.

## Nota sobre "evitar portagens"

`avoidTolls=true` é uma preferência forte, não uma garantia matemática absoluta: a própria documentação da Google indica que o modificador evita portagens "where reasonable". A app mantém `avoidHighways=false`, por isso autoestradas gratuitas continuam permitidas.

## Deployment rápido online

Para o teste mais rápido, usa Streamlit Community Cloud. Consulta `DEPLOY_STREAMLIT.md`.

A versão atual também aceita ficheiros `.xlsx` e lê a API key de `st.secrets["GOOGLE_MAPS_API_KEY"]` quando está alojada online.
