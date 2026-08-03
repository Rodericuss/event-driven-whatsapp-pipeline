# WhatsApp Marketplace Importer

Automação human-in-the-loop que transforma sequências de texto e imagens do
WhatsApp em candidatos estruturados de anúncio. O sistema agrupa eventos
assíncronos, extrai campos com um modelo local, valida o resultado com regras
determinísticas e exige aprovação individual antes de qualquer publicação.

O repositório público usa somente configuração segura e dados sintéticos.
Telefones, JIDs, nomes de grupos, credenciais, mídias e anúncios reais ficam
fora do Git.

## Principais características

- staging persistente em SQLite para callbacks fora de ordem;
- máquina de estados para separar anúncios enviados em sequência;
- processamento independente de vários candidatos;
- extração estruturada com Ollama e JSON Schema;
- validação determinística de preço, ano, categoria e tipo;
- esclarecimento persistente quando faltam dados recuperáveis;
- aprovação individual por reação no chat privado;
- fila serializada para impedir mistura de álbuns;
- idempotência e checkpoints de entrega;
- publicação sem cadastro no site quando essa integração está desativada;
- bloqueio seguro por padrão (`DRY_RUN=true` e destinos desabilitados).

## Arquitetura

```text
WhatsApp
   ↓
plugin OpenClaw
   ↓
staging SQLite e janela de estabilidade
   ↓
ordenação por timestamp original
   ↓
agrupamento em candidatos independentes
   ↓
extração local + validações determinísticas
   ↓
card privado de aprovação
   ↓
fila e publicação idempotente
```

Veja [ARCHITECTURE.md](ARCHITECTURE.md) para os detalhes do fluxo e das travas.

## Configuração local

Requisitos principais:

- Python 3.11 ou superior;
- Node.js compatível com a instalação do OpenClaw;
- OpenClaw com o canal WhatsApp configurado;
- Ollama com um modelo capaz de produzir JSON estruturado.

Crie os arquivos locais:

```bash
cp .env.example .env
cp config/settings.example.json config/settings.local.json
chmod 600 .env config/settings.local.json
```

Preencha `.env` somente na máquina que executará o importador. Destinos reais
devem continuar vazios enquanto o fluxo estiver em desenvolvimento.

A configuração é carregada nesta ordem:

1. caminho definido por `IMPORTER_SETTINGS_PATH`;
2. `config/settings.local.json`;
3. `config/settings.json`, mantido apenas por compatibilidade e testes;
4. `config/settings.example.json`.

As variáveis de ambiente sobrescrevem os campos equivalentes do JSON. O
carregador também lê `.env` sem substituir variáveis já definidas pelo processo.

## Segurança operacional

- `DRY_RUN` começa habilitado;
- entrada e publicação em grupos começam desabilitadas;
- somente chats e grupos explicitamente permitidos são aceitos;
- o grupo de entrada nunca serve como aprovação;
- a publicação exige uma autorização ligada ao ID exato do candidato;
- entregas incertas não são repetidas automaticamente;
- mensagens do WhatsApp são tratadas como dados não confiáveis;
- tokens são lidos do ambiente ou de armazenamento local ignorado;
- pacotes de anúncio e bancos SQLite nunca são versionados.

Não habilite um destino real antes de revisar o diff da configuração e validar o
fluxo em modo-sombra ou com dados sintéticos.

## Dados de runtime

Os diretórios abaixo mantêm apenas `.gitkeep` no repositório:

```text
anuncios/recebendo/
anuncios/pendentes/
anuncios/processados/
anuncios/descartados/
anuncios/erros/
```

Os conteúdos dessas pastas podem incluir textos, fotos, metadados, IDs e
checkpoints privados. Nunca os adicione ao Git, mesmo em fixtures ou exemplos.

## Testes

```bash
python3 -m unittest discover -s tests -v
```

Validações adicionais recomendadas:

```bash
python3 -m compileall -q src tests
node --check openclaw/plugins/whatsapp-marketplace-importer/index.js
bash -n scripts/* openclaw/skills/whatsapp-marketplace-importer/scripts/*
git diff --check
```

Os testes usam IDs e anúncios sintéticos. Nenhuma execução de teste deve enviar
mensagem, cadastrar produto ou depender de credenciais reais.

## Estado da integração com o marketplace

O contrato para cadastro no marketplace permanece no código, mas começa
desabilitado. O fluxo pode publicar sem URL quando o site estiver fora do
escopo. Para habilitar a integração, configure explicitamente o endpoint, o
token e as travas de visibilidade em um ambiente controlado.

## Divulgação responsável

Não abra uma issue pública contendo token, telefone, JID, anúncio, mídia ou log
real. Consulte [SECURITY.md](SECURITY.md) para relatar uma vulnerabilidade.
