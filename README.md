# WhatsApp Marketplace Importer

Automação orientada a eventos que transforma sequências de texto e imagens do
WhatsApp em anúncios estruturados, mantendo uma pessoa no controle da
publicação.

![Diagrama do pipeline orientado a eventos](docs/assets/architecture-pipeline.png)

> A IA propõe. Regras determinísticas validam. Uma pessoa aprova.

## O que este projeto resolve

Em grupos de compra e venda, um lote costuma chegar assim:

```text
texto do anúncio A
fotos do anúncio A
texto do anúncio B
fotos do anúncio B
...
```

No WhatsApp, porém, texto, fotos e vídeos são eventos independentes. Callbacks
podem chegar atrasados ou fora de ordem, vários anúncios podem ser enviados ao
mesmo tempo e uma falha de rede pode deixar a entrega em estado incerto.

Este projeto:

- captura somente uma origem explicitamente autorizada;
- mantém eventos em staging SQLite durante uma janela de estabilidade;
- reordena mensagens pelo timestamp original;
- separa candidatos com uma máquina de estados;
- anexa as imagens posteriores ao candidato correto;
- extrai título, ano, preço, descrição, categoria e tipo com Ollama;
- valida o JSON com schema e regras de negócio;
- pergunta no privado quando falta uma informação recuperável;
- envia um card privado com a quantidade de imagens;
- publica somente depois de uma aprovação individual;
- serializa os álbuns para evitar mistura de fotos;
- impede duplicações com hashes, IDs, locks e checkpoints.

## Segurança por padrão

O repositório público não contém telefones, JIDs, grupos, tokens, anúncios ou
mídias reais. A configuração de exemplo começa com:

- `DRY_RUN=true`;
- entrada em grupo desabilitada;
- publicação pessoal e em grupo desabilitadas;
- marketplace desabilitado;
- destinos e allowlists vazios.

Mensagens do WhatsApp são tratadas como dados não confiáveis. A IA nunca
autoriza publicação, e uma reação no grupo de origem nunca vale como aprovação.

## Arquitetura

```text
Grupo de origem
      ↓
OpenClaw / WhatsApp
      ↓
SQLite staging (janela de 8 segundos)
      ↓
Ordenação + máquina de estados
      ↓
Candidatos independentes
      ↓
Ollama → JSON estruturado
      ↓
JSON Schema + regras determinísticas
      ↓
Card no chat privado
      ↓
👍 aprova | 👎 cancela | dúvida pede esclarecimento
      ↓
Fila serializada → grupo de publicação
```

Detalhes sobre concorrência, idempotência e fronteiras de confiança estão em
[ARCHITECTURE.md](ARCHITECTURE.md).

## Requisitos

- Linux;
- Python 3.11 ou superior;
- Node.js compatível com sua versão do OpenClaw;
- OpenClaw com o canal WhatsApp configurado;
- Ollama com um modelo que produza JSON estruturado;
- SQLite 3 para inspeção operacional opcional.

O fluxo de texto não depende de uma API externa de IA. O modelo roda no Ollama
configurado pela instalação.

## Instalação rápida

Clone o projeto e prepare a configuração local:

```bash
git clone https://github.com/Rodericuss/auto-publication-romildonegocios.git
cd auto-publication-romildonegocios
scripts/bootstrap-local-config
```

O bootstrap:

1. cria `.env` a partir de `.env.example`;
2. cria `config/settings.local.json` a partir do exemplo seguro;
3. aplica permissão `600` aos arquivos privados;
4. cria `config/settings.json -> settings.local.json` para compatibilidade;
5. nunca sobrescreve silenciosamente uma configuração existente.

Valide os defaults antes de editar:

```bash
scripts/validate-local-config
scripts/validate-local-config --public-example
```

## Onde configurar cada coisa

Há duas camadas locais, ambas ignoradas pelo Git:

| Arquivo | Uso |
|---|---|
| `.env` | endpoints, modelo, flags, chats, grupos e token |
| `config/settings.local.json` | catálogo de keywords e configuração estruturada |

As variáveis de ambiente sobrescrevem o JSON. A precedência dos arquivos é:

1. `IMPORTER_SETTINGS_PATH`;
2. `config/settings.local.json`;
3. `config/settings.json`, fallback legado;
4. `config/settings.example.json`, sempre seguro e sem destinos.

O carregador lê `.env`, mas não substitui uma variável que já exista no processo
ou no serviço systemd.

## Configurando a IA

O caminho operacional atual suporta modelos servidos pelo Ollama. Configure em
`.env`:

```dotenv
OLLAMA_PROVIDER=ollama
OLLAMA_ENDPOINT=http://127.0.0.1:11434
OLLAMA_EXTRACTION_MODEL=qwen3-agent
OLLAMA_TIMEOUT_SECONDS=120
```

### Parâmetros disponíveis

| Variável | Exemplo | Efeito |
|---|---|---|
| `OLLAMA_PROVIDER` | `ollama` | Provedor suportado pelo extrator atual |
| `OLLAMA_ENDPOINT` | `http://127.0.0.1:11434` | Servidor Ollama utilizado |
| `OLLAMA_EXTRACTION_MODEL` | `qwen3-agent` | Modelo textual carregado pelo Ollama |
| `OLLAMA_TIMEOUT_SECONDS` | `120` | Limite de espera por extração |
| `REDACTED_TERMS` | `Empresa Exemplo,Vendedor Exemplo` | Termos removidos e rejeitados na descrição |

Para trocar de modelo:

1. instale ou baixe o modelo no Ollama;
2. altere apenas `OLLAMA_EXTRACTION_MODEL`;
3. execute os testes;
4. envie anúncios sintéticos em modo seguro;
5. verifique preços, anos, categoria e tipo antes de habilitar um destino real.

O extrator usa temperatura zero, seed fixa e JSON Schema para reduzir variação.
Mesmo assim, a saída continua sujeita a validações determinísticas e aprovação
humana.

### Keywords de tipos aceitos

O filtro inicial usa `item_keywords` em `config/settings.local.json`:

```json
{
  "item_keywords": [
    "trator",
    "escavadeira",
    "retroescavadeira",
    "caminhão"
  ]
}
```

Adicione termos de maneira conservadora. Uma keyword muito genérica aumenta o
risco de transformar conversas comuns em candidatos.

### Validação visual

A validação visual não participa da decisão operacional. Ela foi retirada porque
modelos visuais rejeitavam fotos corretas de cabine, painel, peças e detalhes
internos. Somente imagens são anexadas ao anúncio; vídeos são ignorados.

## Configurando chats e grupos

Esta versão trabalha com:

- um ou mais chats pessoais explicitamente permitidos;
- um grupo de origem por instalação;
- um grupo de publicação, diferente da origem;
- um chat pessoal responsável por esclarecimentos e aprovações.

Use IDs fictícios nos exemplos e coloque os valores reais somente em `.env`:

```dotenv
OPENCLAW_PERSONAL_CHAT_ID=5500000000000
OPENCLAW_ALLOWED_CHAT_IDS=5500000000000

SOURCE_GROUP_ENABLED=true
SOURCE_GROUP_SHADOW_MODE=true
SOURCE_GROUP_NAME=GRUPO DE ORIGEM
SOURCE_GROUP_JID=100000000000000001@g.us
APPROVAL_CHAT_ID=5500000000000

GROUP_PUBLICATION_ENABLED=true
GROUP_PUBLICATION_CHANNEL=whatsapp
PUBLICATION_GROUP_NAME=GRUPO DE PUBLICAÇÃO
PUBLICATION_GROUP_JID=100000000000000002@g.us
```

Os números acima são sintéticos. Um JID de grupo deve terminar em `@g.us`.

### Regras obrigatórias

- `APPROVAL_CHAT_ID` precisa estar em `OPENCLAW_ALLOWED_CHAT_IDS`;
- origem e publicação não podem usar o mesmo JID;
- o nome não autoriza nada: a comparação é feita pelo JID;
- o grupo de origem não recebe respostas nem reações automáticas;
- somente o card correspondente no privado pode autorizar um candidato;
- não use wildcard para aceitar grupos desconhecidos.

O comando abaixo falha antes da inicialização quando alguma dessas regras está
inconsistente:

```bash
scripts/validate-local-config
```

### Alinhando a configuração do OpenClaw

O `.env` configura o importador, mas o canal WhatsApp do OpenClaw também precisa
usar a mesma allowlist. A forma mínima esperada é equivalente a:

```json
{
  "channels": {
    "whatsapp": {
      "dmPolicy": "allowlist",
      "allowFrom": ["5500000000000"],
      "groupPolicy": "allowlist",
      "groupAllowFrom": ["*"],
      "groups": {
        "100000000000000001@g.us": {
          "requireMention": false
        }
      },
      "ackReaction": {
        "group": "never"
      }
    }
  }
}
```

E o plugin deve receber valores equivalentes a:

```json
{
  "projectRoot": "/caminho/absoluto/para/o/projeto",
  "allowedChatIds": ["5500000000000"],
  "dryRun": false,
  "groupIntake": {
    "enabled": true,
    "groupJid": "100000000000000001@g.us",
    "approvalChatId": "5500000000000",
    "shadowMode": true
  }
}
```

Todos os valores são exemplos sintéticos. Não versione seu arquivo real do
OpenClaw nem o estado de autenticação do WhatsApp/Baileys.

### Trocando os grupos com segurança

1. mantenha o gateway sem ingerir novos eventos durante a alteração;
2. faça backup de `.env`, `settings.local.json` e da configuração do OpenClaw;
3. altere origem, aprovação e destino nas duas camadas;
4. confirme que origem e destino são diferentes;
5. execute `scripts/validate-local-config`;
6. execute a suíte automatizada;
7. teste primeiro com `SOURCE_GROUP_SHADOW_MODE=true`;
8. só habilite publicação após verificar cards, fotos e checkpoints.

Para mais de um grupo de origem, prefira instâncias isoladas com configuração e
staging próprios. Esta implementação restringe deliberadamente cada instância a
um JID de origem para manter a fronteira de confiança auditável.

## Flags de publicação

```dotenv
DRY_RUN=true
PERSONAL_PUBLICATION_ENABLED=false
GROUP_PUBLICATION_ENABLED=false
MARKETPLACE_ENABLED=false
MARKETPLACE_VISIBLE=false
```

Mude uma trava por vez. O modo público de exemplo nunca permite escrita.

Quando `MARKETPLACE_ENABLED=false`, um anúncio aprovado pode seguir pelo fluxo
sem site e sem URL. Para habilitar a API interna, configure:

```dotenv
MARKETPLACE_ENABLED=true
MARKETPLACE_INTERNAL_URL=http://127.0.0.1:4000
MARKETPLACE_API_PATH=/api/internal/imported-products
MARKETPLACE_DRY_RUN_ONLY=true
MARKETPLACE_VISIBLE=false
MARKETPLACE_INTERNAL_API_TOKEN=
MARKETPLACE_FLY_APP=
```

O token deve existir somente no ambiente ou em `.marketplace-token` com modo
`600`.

## Como usar

Depois que o plugin e o canal estiverem configurados:

1. envie texto e fotos no grupo de origem;
2. aguarde a janela de estabilidade;
3. receba o card no chat privado;
4. confira título, preço, tipo e quantidade de imagens;
5. responda à pergunta se houver esclarecimento pendente;
6. reaja com 👍 para publicar ou 👎 para cancelar.

Um candidato aguardando aprovação não bloqueia os próximos.

## Estados e checkpoints

Cada candidato mantém arquivos privados em `anuncios/pendentes/<uuid>/`, como:

- `mensagem-original.txt`;
- `metadata.json`;
- `anuncio-extraido.json`;
- `clarification.json`;
- `status.json`;
- `whatsapp-group-album-publication.json`;
- fotos e hashes SHA-256.

Uma entrega `complete/sent` nunca deve ser repetida. Uma falha só pode ser
reexecutada quando o checkpoint comprova ausência de ID de mensagem e falha
anterior ao aceite pelo provedor.

## Dados de runtime

O Git mantém apenas `.gitkeep` nestas pastas:

```text
anuncios/recebendo/
anuncios/pendentes/
anuncios/processados/
anuncios/descartados/
anuncios/erros/
```

Nunca transforme um pacote real em fixture. Os testes usam anúncios, telefones,
JIDs, imagens e IDs totalmente sintéticos.

## Testes

Execute a suíte completa:

```bash
python3 -m unittest discover -s tests -v
```

Validações adicionais:

```bash
scripts/validate-local-config --public-example
python3 -m compileall -q src tests scripts
node --check openclaw/plugins/whatsapp-marketplace-importer/index.js
git diff --check
```

O checkpoint atual possui 124 testes unitários, de integração, regressão e
segurança.

## Estrutura do projeto

```text
config/       schemas, catálogo e configuração pública segura
openclaw/     plugin e skill de integração
scripts/      comandos operacionais e validações
src/          ingestão, staging, extração, fila e publicação
tests/        testes e fixtures sintéticos
anuncios/     runtime privado ignorado pelo Git
docs/assets/  recursos públicos do README
```

## Limitações atuais

- somente imagens entram nos anúncios; vídeos são ignorados;
- há um grupo de origem por instância;
- a validação visual está desabilitada;
- o marketplace começa desabilitado;
- atualizações do OpenClaw podem exigir revalidar patches de álbum e reações;
- operação real exige monitoramento dos checkpoints e do gateway.

## Segurança

Não abra uma issue pública contendo token, telefone, JID, nome de grupo, anúncio,
mídia ou log real. Use o fluxo descrito em [SECURITY.md](SECURITY.md).

## Licença

Distribuído sob a licença [MIT](LICENSE).
