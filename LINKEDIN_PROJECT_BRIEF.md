# Briefing para publicação no LinkedIn

## Como usar este arquivo

Anexe este documento ao ChatGPT e peça que ele transforme o conteúdo em uma
publicação para LinkedIn. Os fatos abaixo já foram revisados. Não exponha
telefones, identificadores internos do WhatsApp, tokens, IDs de mensagens,
credenciais ou caminhos locais.

## Resumo em uma frase

Construí uma automação com IA e aprovação humana que transforma anúncios de
máquinas recebidos em um grupo do WhatsApp em publicações estruturadas, separa
corretamente texto e álbuns enviados em sequência e só publica depois da
confirmação individual do responsável.

## O problema

Os anúncios chegam pelo WhatsApp em um formato pouco estruturado:

```text
texto da máquina A
várias fotos da máquina A
texto da máquina B
várias fotos da máquina B
...
```

Na prática, o comportamento é mais difícil do que parece:

- textos, fotos e vídeos chegam como eventos separados;
- callbacks de imagens podem chegar atrasados e fora de ordem;
- vários anúncios são enviados quase ao mesmo tempo;
- um texto pode complementar o anúncio anterior ou iniciar outro;
- mensagens repetidas não podem gerar publicações duplicadas;
- preços brasileiros, quilometragem e horas de uso podem ser confundidos;
- o sistema não pode responder no grupo de origem;
- nenhuma publicação real pode acontecer sem aprovação humana.

O objetivo foi reduzir o trabalho manual sem entregar decisões sensíveis
completamente à IA.

## A solução construída

O fluxo atual funciona assim:

```text
Grupo de origem no WhatsApp
        ↓
OpenClaw captura texto e mídia
        ↓
Staging persistente em SQLite por 8 segundos
        ↓
Ordenação pelo horário original das mensagens
        ↓
Máquina de estados separa candidatos independentes
        ↓
IA local extrai título, ano, preço, descrição, categoria e tipo
        ↓
Validação determinística confere o JSON e as regras de negócio
        ↓
Card de aprovação enviado somente ao chat privado
        ↓
👍 publica | 👎 cancela | dúvida pede esclarecimento
        ↓
Fila serializa a publicação final no grupo de destino
```

Cada anúncio recebe um identificador próprio e um pacote auditável contendo o
texto original, metadados, fotos ordenadas, resultado extraído, validação,
status e checkpoints de publicação.

## Tecnologias e conceitos aplicados

- Python para ingestão, agrupamento, validação, fila e publicação;
- JavaScript/Node.js para o plugin do OpenClaw;
- SQLite para staging persistente e fila;
- OpenClaw como integração com o WhatsApp;
- Baileys para suporte ao formato nativo de álbuns do WhatsApp;
- Ollama com modelo local para extração estruturada do texto;
- JSON Schema e validações determinísticas;
- Bash para comandos operacionais versionados;
- systemd para manter o gateway disponível;
- testes unitários, de integração, regressão e segurança;
- idempotência, checkpoints, locks, allowlists e human-in-the-loop.

O projeto também possui uma integração preparada com um marketplace em
Elixir/Phoenix, mas o cadastro no site está deliberadamente desativado no fluxo
operacional atual. Hoje a publicação acontece sem link para o site.

## Desafios técnicos mais interessantes

### 1. Concorrência e eventos fora de ordem

Em um teste real, 11 imagens de uma retroescavadeira chegaram ao callback
depois dos eventos observados de outros anúncios. O agrupador antigo deixou a
retroescavadeira sem fotos e anexou suas 11 imagens às 5 imagens de uma
escavadeira seguinte.

A solução foi substituir o conceito de “candidato atual” por um staging
persistente. Os eventos aguardam uma janela curta de estabilidade, são
ordenados pelo timestamp original do WhatsApp e só depois passam pela máquina
de estados. Um teste de regressão reproduz exatamente a distribuição
`11 / 5 / 5 / 5`.

### 2. Separar mensagens parecidas sem perder complementos

O agrupador precisa distinguir, por exemplo, dois veículos da mesma marca mas
de anos ou modelos diferentes. Ao mesmo tempo, um texto curto como o modelo e
o ano pode ser apenas um complemento do anúncio aberto.

A comparação passou a considerar tokens de modelo, ano, sinais comerciais e
termos genéricos. Textos incompatíveis criam candidatos diferentes; detalhes
compatíveis são somados sem alterar o texto original.

### 3. Aprovação humana sem quebrar o paralelismo

Um anúncio aguardando aprovação não pode impedir a chegada dos próximos. Cada
candidato é processado independentemente e pode ficar aguardando informação ou
aprovação enquanto outros continuam sendo capturados e validados.

O responsável recebe no privado um card com título, preço, tipo e quantidade
de imagens. Uma reação de 👍 autoriza somente aquele anúncio; 👎 cancela. A
publicação final é serializada para não misturar álbuns.

### 4. Idempotência e entregas incertas

A automação evita duplicidade em várias camadas:

- ID da mensagem;
- hash SHA-256 da mídia;
- texto repetido no mesmo dia;
- identificador da importação;
- chave idempotente da publicação;
- checkpoint com o ID devolvido pelo WhatsApp.

Se a entrega for incerta, o sistema bloqueia o retry automático. Só é seguro
repetir quando o checkpoint comprova que a falha aconteceu antes de o provedor
aceitar a mensagem.

### 5. Saber quando não usar IA

Foi testada uma validação visual para conferir os álbuns, mas o modelo rejeitou
fotos corretas de cabine, painel, peças e detalhes internos. Em vez de manter
uma camada que acrescentava falsa confiança, a validação visual foi retirada
do caminho operacional.

A IA local continua responsável pela proposta de extração textual. As regras
determinísticas e a aprovação humana continuam responsáveis pela segurança.
Essa decisão foi tão importante quanto qualquer funcionalidade adicionada.

### 6. Segurança na integração com grupos

O sistema admite somente um grupo de origem explicitamente configurado. O
agente principal é interrompido antes de responder, reações automáticas ficam
desativadas no grupo e cartões, dúvidas e erros são encaminhados somente ao
chat privado autorizado.

O grupo de origem e o grupo de publicação são destinos diferentes. Uma reação
recebida no grupo de origem nunca vale como autorização.

## Resultados mensuráveis

- replay de um dia real com 24 mensagens de anúncio;
- identificação correta de 22 anúncios únicos;
- evolução do filtro de 16 para 22 anúncios reconhecidos nesse conjunto;
- mensagens de frete e ruído continuaram sendo ignoradas;
- teste determinístico de callbacks fora de ordem com `11 / 5 / 5 / 5` fotos;
- 115 testes automatizados passando no último checkpoint registrado;
- modo-sombra usado antes de ativar a entrada do grupo real;
- primeiro anúncio real do novo fluxo capturado, aprovado e publicado;
- texto original e trilha de auditoria preservados;
- nenhuma publicação automática sem confirmação individual.

## Falhas reais que viraram melhorias

- imagens atrasadas foram misturadas entre dois equipamentos;
- um cartão pendente fez novos anúncios parecerem respostas de esclarecimento;
- formatos como `240,000,00` revelaram lacunas no parser de preço;
- quilômetros e horas contendo “mil” quase foram interpretados como valor;
- a validação visual produziu falsos positivos e foi revertida;
- uma proteção antiga aceitava publicação apenas com grupos totalmente
  desabilitados e precisou evoluir para reconhecer uma allowlist segura;
- o envio genérico de várias mídias não produzia um álbum nativo e exigiu uma
  integração específica com o adaptador do WhatsApp.

O projeto foi evoluído a partir dessas evidências, sempre adicionando testes de
regressão antes da próxima ativação.

## O que considero mais valioso neste projeto

O ponto forte não é simplesmente “usar IA para ler WhatsApp”. É a combinação de
IA com engenharia de confiabilidade:

- a IA propõe;
- regras determinísticas validam;
- o estado persistente permite recuperação;
- idempotência evita duplicações;
- o humano aprova a ação irreversível;
- logs e checkpoints explicam o que aconteceu.

Isso transforma uma demonstração de IA em uma automação que pode operar sobre
eventos reais com limites claros.

## Estado atual e limitações honestas

- o cadastro automático no site está desativado;
- a publicação atual não inclui URL de produto;
- vídeos são ignorados; apenas imagens entram nos anúncios;
- a validação visual não participa da decisão;
- mídias históricas antigas só podem ser republicadas se os arquivos reais
  ainda estiverem disponíveis;
- atualizações do adaptador do WhatsApp podem exigir revalidar o patch de álbum;
- o primeiro anúncio real validou o fluxo completo, mas o monitoramento e os
  testes com novos lotes continuam importantes.

## Minha participação

Use apenas formulações verdadeiras na publicação. Uma descrição possível:

> Idealizei o fluxo, defini as regras de segurança e aprovação, conduzi os
> testes reais, investiguei falhas, refinei os critérios de agrupamento e usei
> agentes de IA como parceiros na implementação e validação do código.

Se for relevante para a narrativa, vale mencionar de forma transparente que o
projeto foi desenvolvido em colaboração com ferramentas de IA. O diferencial
está nas decisões, nos testes, na supervisão e na transformação dos erros reais
em requisitos de engenharia.

## Possíveis ângulos para a publicação

### Opção 1 — Case técnico

Foco em eventos assíncronos, máquina de estados, staging persistente,
idempotência e aprovação humana.

### Opção 2 — Aprendizado com IA

Foco na lição de que IA útil em produção precisa de validação determinística,
limites, observabilidade e capacidade de rollback.

### Opção 3 — Jornada de produto

Foco na evolução de uma tarefa manual do WhatsApp para um fluxo auditável, com
falhas reais, decisões incrementais e melhoria contínua.

## Prompt pronto para enviar ao ChatGPT

```text
Com base exclusivamente no briefing anexado, escreva uma publicação em
português para o LinkedIn sobre este projeto.

Objetivos:
- mostrar capacidade prática de engenharia de software e automação com IA;
- contar uma história concreta, não parecer propaganda genérica;
- destacar o problema de eventos assíncronos, a solução com staging/SQLite,
  idempotência, aprovação humana e os 115 testes;
- mencionar uma falha real e o aprendizado obtido;
- explicar que a IA faz extração, mas regras determinísticas e um humano
  controlam a publicação;
- ser transparente sobre o uso de ferramentas de IA no desenvolvimento;
- terminar com uma pergunta que incentive conversa sobre automação confiável.

Tom: profissional, humano, curioso e técnico sem ficar excessivamente formal.
Tamanho: entre 1.200 e 1.800 caracteres.
Formato: parágrafos curtos, poucos emojis e no máximo cinco hashtags.

Não invente resultados, não diga que a validação visual está ativa, não diga
que o site está integrado no fluxo atual e não inclua telefones, IDs internos,
tokens, caminhos locais ou nomes de grupos privados.

Depois da primeira versão, forneça também:
1. uma abertura alternativa mais forte;
2. uma versão curta de até 700 caracteres;
3. cinco sugestões de título para um carrossel técnico.
```

## Imagens que podem acompanhar a publicação

- diagrama simplificado do fluxo, sem identificadores privados;
- screenshot do card de aprovação com telefone e código borrados;
- screenshot de um álbum publicado, com dados comerciais sensíveis ocultos;
- terminal mostrando `115 tests ... OK`;
- trecho anonimizado do teste `11 / 5 / 5 / 5`.

Antes de publicar qualquer screenshot, ocultar telefones, JIDs, IDs de
mensagem, caminhos locais, tokens e nomes que não tenham autorização para
aparecer publicamente.
