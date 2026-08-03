# Schemas persistidos

Cada pacote fica em `anuncios/pendentes/<import-id>/`.

`mensagem-original.txt` contém os bytes UTF-8 do texto entregue ao importador,
sem correção ou resumo.

`metadata.json` contém:

- `import_id`, origem, chat e remetente;
- IDs de todas as mensagens associadas;
- timestamps de texto e mídia;
- contagem e lista ordenada de mídias;
- para cada mídia: sequência, nome local, SHA-256, MIME e `message_id`.

`status.json` sempre mantém:

- validação e extração refletem o processamento local;
- registro, upload e publicação permanecem `false`;
- `dry_run: true`;
- erros e avisos explícitos;
- estado de captura ou revisão.

Na fase de extração também são criados:

- `anuncio-extraido.json`: proposta bruta estruturada do modelo;
- `validacao.json`: validação determinística, erros, modelo e fonte do catálogo;
- `clarification.json`: pergunta pendente, campo, código curto, tentativas e
  histórico auditável de respostas. Também guarda a confirmação final de
  publicação.
- `review-overrides.json`: respostas confirmadas pelo usuário, sem alterar
  `mensagem-original.txt`;
- `analise-imagens.json`: proposta e validação visual, modelo usado, confiança,
  contradições e índices de imagens irrelevantes.
- `marketplace-request.json`: payload dry-run sem credenciais, criado somente
  após validação textual e visual.
- `marketplace-response.json`: envelope auditável da validação da API local,
  com plano ordenado de uploads e todas as escritas bloqueadas.
- `marketplace-live-response.json`: checkpoint da criação inicialmente
  invisível, respostas dos uploads e finalização com a visibilidade configurada.
  Não contém token e mantém a publicação de WhatsApp separada.
- `whatsapp-personal-album-publication.json`: checkpoint do álbum enviado ao
  chat pessoal, com legenda, mídias ordenadas, chave de idempotência, estado,
  timestamp, resposta e `message_id`. Não contém credenciais.
- `whatsapp-group-album-publication.json`: checkpoint do álbum enviado ao grupo
  de saída autorizado, com nome e JID literais, mídias ordenadas, chave de
  idempotência, estado, timestamp, resposta e `message_id`. Não contém
  credenciais. No fallback sem cadastro, contém `without_site=true`,
  `product_id=null` e legenda sem URL; o `status.json` mantém
  `site_registration_pending=true`.

Nenhum arquivo contém token, credencial do WhatsApp ou credencial AWS.
