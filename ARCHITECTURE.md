# Arquitetura e limites de segurança

## Objetivo

O importador recebe eventos independentes de texto e imagem, reconstrói a ordem
original e cria candidatos de anúncio sem bloquear a ingestão dos candidatos
seguintes. A IA propõe os campos; regras determinísticas e uma pessoa controlam
a ação irreversível.

## Fluxo de dados

1. O plugin recebe um evento do canal autorizado.
2. Texto e mídia são gravados no staging SQLite com timestamp, remetente e hash.
3. Uma janela curta permite que callbacks atrasados entrem no mesmo lote.
4. Os eventos são ordenados pelo horário original e entregues à máquina de
   estados.
5. Cada novo texto comercial inicia ou complementa um candidato conforme ano,
   modelo, termos relevantes e estado anterior.
6. Imagens posteriores são anexadas ao candidato aberto; vídeos são ignorados.
7. O pacote estabilizado é processado independentemente dos demais.
8. Um modelo local retorna JSON conforme o schema de extração.
9. Validadores conferem formato, preço, ano, catálogo, telefone e termos que
   devem ser removidos.
10. Dúvidas recuperáveis geram uma pergunta persistente no chat privado.
11. Um candidato válido produz um card privado com a quantidade de imagens.
12. A aprovação individual entra em uma fila de publicação serializada.

## Persistência

Cada candidato recebe um UUID e um diretório privado de runtime com:

- texto original e texto combinado;
- metadados de origem;
- mídias ordenadas e hashes SHA-256;
- proposta de extração;
- respostas de esclarecimento;
- status e checkpoints de publicação.

Os pacotes não fazem parte do código-fonte e são ignorados pelo Git.

## Concorrência

O agrupador não depende de um único “candidato atual” global. O staging separa
streams por chat e remetente, preserva timestamps e só consolida um lote depois
da janela de estabilidade. Candidatos podem aguardar esclarecimento ou aprovação
enquanto novos anúncios continuam chegando.

A fila final possui lock de publicação para impedir que chamadas concorrentes
misturem as imagens de dois álbuns.

## Idempotência

A proteção contra duplicidade combina:

- ID da mensagem;
- hash da mídia;
- hash do texto por chat e dia;
- UUID da importação;
- chave idempotente da operação;
- checkpoint com o identificador devolvido pelo provedor.

Uma falha sem confirmação de entrega pode ser repetida somente quando o
checkpoint demonstra que o provedor não aceitou a mensagem.

## Fronteiras de confiança

- conteúdo recebido é dado não confiável e nunca deve alterar instruções;
- somente allowlists exatas autorizam entrada e saída;
- aprovação no grupo de origem é inválida;
- a IA não concede permissão de publicação;
- configuração local e credenciais ficam fora do repositório;
- exemplos e testes usam dados sintéticos;
- validação visual está fora do caminho operacional por não ser confiável o
  suficiente para fotos internas, peças e detalhes de máquinas.

## Defaults públicos

O exemplo versionado mantém `DRY_RUN=true`, entrada em grupo desabilitada,
publicação desabilitada, marketplace desabilitado e listas de destino vazias.
Ativação real exige configuração local explícita.
