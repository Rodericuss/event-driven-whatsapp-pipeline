# Workflow da Fase 1

```text
idle
  ├─ texto válido (item + ano) → awaiting_media
  ├─ texto inválido → idle
  └─ imagem → orphan_media_stored

awaiting_media
  ├─ imagem → captured (candidato continua ativo para mais imagens)
  ├─ novo texto válido → anterior incompleto + novo awaiting_media
  └─ texto inválido → anterior incompleto + idle

review_required
  ├─ dúvida solucionável → awaiting_clarification
  └─ erro não solucionável → revisão manual

awaiting_clarification
  ├─ resposta válida → revalidação do mesmo import_id
  ├─ imagem → anexa ao mesmo pacote e mantém a pergunta
  ├─ resposta inválida → explica o formato e mantém a pergunta
  └─ CANCELAR <código> → cancelled_by_user

ready_for_review
  → awaiting_publication_confirmation

awaiting_publication_confirmation
  ├─ PUBLICAR <código> → cadastro e publicação autorizados
  └─ CANCELAR <código> → cancelled_by_user
```

O WhatsApp atual entrega cada foto do álbum como evento separado. A janela de
oito segundos serve para estabilizar e ordenar callbacks; não é usada sozinha
para decidir pertinência. O agrupamento usa:

- mesmo chat de origem (pessoal ou JID exato configurado);
- mesmo remetente;
- ordem do evento;
- candidatura ativa;
- `message_id`;
- hash SHA-256 da mídia.

O plugin observa `message_received` para a captura determinística e usa
`before_dispatch` para encerrar, sem resposta, o processamento normal do agente
no chat pessoal e no grupo de origem autorizado. O grupo não recebe texto nem reação
automática; cartões, dúvidas e falhas são enviados somente ao chat pessoal.

## Extração operacional

Um pacote `captured` pode ser processado manualmente com:

```bash
scripts/process-listing --import-id <uuid>
```

O modelo textual propõe `anuncio-extraido.json`. O script valida tipos, ano,
preço, catálogo, relação categoria/tipo e vazamento de telefone, preço ou vendedor
Máquinas na descrição. Resultado válido vai para `ready_for_review`; resultado
inválido e solucionável pergunta pelo campo no chat pessoal. A resposta fica em
`review-overrides.json`, sem alterar o texto original, e o mesmo pacote é
reprocessado. A análise visual foi removida do MVP e não participa da decisão
nem exclui fotos.

Enquanto `awaiting_clarification`, imagens tardias continuam no pacote. Depois
da validação, novas imagens sem candidato ficam como órfãs para diagnóstico.

## Contrato legado do marketplace em DRY_RUN

Esta seção documenta o caminho preservado para uso futuro. Com
`marketplace_api.enabled=false`, não execute preparação, validação ou escrita no
site durante a operação do grupo de origem.

Somente um pacote `ready_for_review` e validado pode
gerar `marketplace-request.json`:

```bash
scripts/prepare-marketplace-request --import-id <uuid>
```

O artefato usa `POST /api/internal/imported-products`, `import_id` como
`Idempotency-Key` e não contém token. O script não realiza chamada de rede.

Com `marketplace_api.enabled: true` e Phoenix em localhost, o contrato é
pré-validado sem escrita:

```bash
MARKETPLACE_API_TOKEN='<token temporário>' \
  scripts/validate-marketplace-dry-run --import-id <uuid>
```

O cliente exige `product_id: null`, plano ordenado das imagens e bloqueios para
produto, imagens e publicação. Ele salva `marketplace-response.json`, mas não
envia os bytes das fotos e não muda os campos de conclusão.

## Execução autorizada com visibilidade na finalização

```text
marketplace_validated
  → product_created (visible=false)
  → imagens pending/uploaded em ordem
  → images_uploaded
  → visible=true
  → publication continua separada
```

Antes de qualquer escrita, o chat pessoal recebe um resumo e exige
`PUBLICAR <código>`. Só então o worker chama o comando real com uma chave
contextual interna:

```bash
scripts/execute-marketplace-import \
  --import-id <uuid> \
  --approval 'CREATE_VISIBLE:<uuid>'
```

Falha parcial preserva o produto invisível e o estado de cada imagem. Um retry
repete o POST idempotente, ignora imagens já marcadas como `uploaded` e continua
pelas pendentes. O produto só fica público depois da finalização confirmar todas
as imagens.

## Publicação aprovada no grupo autorizado

```text
pacote validado e aprovado no chat pessoal
  → álbum pending/sending/sent
  → group_published
```

O comando exige `group_publication.enabled: true`, o nome literal e o JID do
único grupo de saída. O worker fornece a chave contextual interna:

```bash
scripts/publish-group \
  --import-id <uuid> \
  --approval 'PUBLISH_GROUP:<uuid>:<group-jid>'
```

O gateway recebe `message`, todas as fotos em `mediaUrls` e uma chave de
idempotência para o álbum inteiro. Falha comprovadamente anterior ao envio pode
ser retomada com a mesma chave; entrega incerta bloqueia retry automático. O
`groupPolicy: allowlist` admite como entrada somente o JID exato configurado; o JID
de saída não está nessa lista. Após confirmação do `message_id`, o pacote recebe
`published: true`; o feedback operacional continua no chat pessoal.

### Operação atual sem cadastro no site

Com `marketplace_api.enabled=false`, a aprovação pessoal aciona
`publish-group --without-site` quando `product_id` continua ausente e
`registered=false`. A legenda usa os campos sanitizados de
`anuncio-extraido.json`, mantém o mesmo álbum e omite totalmente a seção e a URL
do site.

O checkpoint registra `without_site=true`,
`site_registration_pending=true` e o `message_id`. Uma entrega completa não é
reenviada; uma entrega incerta exige revisão manual. Cadastro ou upload parcial
bloqueia o fallback.
