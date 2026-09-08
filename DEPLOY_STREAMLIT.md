# Publicar no Streamlit Community Cloud

## 1. Criar um repositório no GitHub
Cria um repositório, por exemplo `route-optimizer`, e coloca lá todos os ficheiros desta pasta.

Não publiques um ficheiro `.streamlit/secrets.toml` com a tua chave. O `.gitignore` já está preparado para o excluir.

## 2. Publicar no Streamlit
1. Abre https://share.streamlit.io
2. Entra com GitHub.
3. Clica em **Create app**.
4. Seleciona o repositório `route-optimizer`.
5. Branch: `main`.
6. Main file path: `app.py`.
7. Antes de finalizar, abre **Advanced settings / Secrets**.
8. Adiciona:

```toml
GOOGLE_MAPS_API_KEY = "A_TUA_CHAVE_GOOGLE"
```

9. Clica em **Deploy**.

A app ficará disponível num endereço do tipo:

`https://nome-da-app.streamlit.app`

## 3. Google Cloud
No projeto Google Cloud:
- billing ativo;
- Routes API ativa;
- API key criada;
- restringir a chave à Routes API.

Para este protótipo, a chave fica apenas no servidor Streamlit através dos Secrets.

## 4. Teste
A app já abre com as moradas de teste preenchidas.

Clica em **CALCULAR AS DUAS ROTAS** para comparar:
- rota aberta: termina no cliente que produz o menor tempo total;
- rota fechada: visita todos e regressa à origem.

Em ambos os casos mostra:
- ordem das visitas;
- distância total;
- duração total;
- distância e duração por troço;
- links para abrir a rota no Google Maps;
- preferência por evitar portagens.
