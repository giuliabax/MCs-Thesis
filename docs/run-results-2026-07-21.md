# Risultati della prima esecuzione completa sui 18 progetti

**Run consolidata:** `data/runs/20260721T235416Z-consolidated`
**Data:** 21–22 luglio 2026
**Modello:** `qwen/qwen3.5-9b` (Q4_K_M) in locale via LM Studio, RTX 3070 8 GB
**Ground truth:** `data/ground_truth/participium_implemented_stories.yaml`
**Base di valutazione:** `openapi_documentation` (30 requisiti PT01–PT30 × 18 progetti = 540 celle)

---

## 1. Metriche aggregate

| Metrica | Macro (media sui progetti) | Micro (su tutte le celle) |
| --- | --- | --- |
| Precision | 0.847 | 0.837 |
| Recall | 0.758 | 0.748 |
| **F1** | **0.775** | **0.790** |

Mediane sui progetti: precision 0.857, recall 0.801, F1 0.789.

**Matrice di confusione complessiva:** TP = 261, FP = 51, FN = 88, TN = 140.

Il comportamento del sistema è **conservativo**: quando afferma che un requisito è
implementato ha ragione nell'84% dei casi, ma non riconosce una quota rilevante delle
implementazioni effettive (88 falsi negativi contro 51 falsi positivi). L'errore dominante
è quindi l'omissione, non l'allucinazione.

---

## 2. Risultati per progetto

Ordinati per numero di team. La colonna *Spec* indica la provenienza del file
`swagger.yaml` usato come input (vedi §4).

| Progetto | Operazioni | Spec | Precision | Recall | F1 | TP | FP | FN | TN | Run di origine |
| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| team01 | 41 | team | 0.800 | 0.889 | 0.842 | 16 | 4 | 2 | 8 | `20260721T172539Z` |
| team02 | 28 | ricostruito | 0.667 | 1.000 | 0.800 | 12 | 6 | 0 | 12 | `20260721T172539Z` |
| team03 | 41 | team | 0.773 | 0.895 | 0.829 | 17 | 5 | 2 | 6 | `20260721T172539Z` |
| team04 | 9 | ricostruito | 1.000 | 0.182 | 0.308 | 4 | 0 | 18 | 8 | `20260721T182119Z` |
| team05 | 17 | team | 0.917 | 0.524 | 0.667 | 11 | 1 | 10 | 8 | `20260721T183216Z` |
| team06 | 28 | ricostruito | 0.867 | 0.684 | 0.765 | 13 | 2 | 6 | 9 | `20260721T184552Z` |
| team07 | 38 | team | 0.917 | 0.524 | 0.667 | 11 | 1 | 10 | 8 | `20260721T190730Z` |
| team08 | 41 | ricostruito | 0.789 | 0.682 | 0.732 | 15 | 4 | 7 | 4 | `20260721T192137Z` |
| team09 | 35 | team | 0.778 | 0.778 | 0.778 | 14 | 4 | 4 | 8 | `20260721T194143Z` |
| team10 | 49 | team | 0.857 | 0.947 | 0.900 | 18 | 3 | 1 | 8 | `20260721T225427Z` |
| team11 | 43 | ricostruito | 0.857 | 0.632 | 0.727 | 12 | 2 | 7 | 9 | `20260721T201200Z` |
| team12 | 34 | ricostruito | 0.737 | 0.737 | 0.737 | 14 | 5 | 5 | 6 | `20260721T202726Z` |
| team13 | 51 | team | 0.826 | 0.905 | 0.864 | 19 | 4 | 2 | 5 | `20260721T232340Z` |
| team14 | 28 | team | 0.789 | 0.882 | 0.833 | 15 | 4 | 2 | 9 | `20260721T205445Z` |
| team15 | 33 | ricostruito | 0.929 | 0.619 | 0.743 | 13 | 1 | 8 | 8 | `20260721T212157Z` |
| team16 | 50 | team | 0.929 | 1.000 | 0.963 | 26 | 2 | 0 | 2 | `20260721T213748Z` |
| team17 | 29 | ricostruito | 0.944 | 0.944 | 0.944 | 17 | 1 | 1 | 11 | `20260721T215932Z` |
| team18 | 31 | team | 0.875 | 0.824 | 0.848 | 14 | 2 | 3 | 11 | `20260721T221622Z` |

Migliore: **team16** (F1 0.963). Peggiore: **team04** (F1 0.308), discusso in §3.

Totale operazioni analizzate sui 18 progetti: **626**.

---

## 3. Caveat 1 — team04 è un outlier strutturale

team04 ottiene precision 1.000 ma recall 0.182 (4 TP, 18 FN): riconosce correttamente
tutto ciò che dichiara, ma non vede quasi nulla.

La causa non è il modello, è il **metodo applicato a quell'architettura**. team04 è
un'applicazione **Next.js (App Router)**: la quasi totalità della logica applicativa è
implementata tramite *Server Actions*, che non sono endpoint REST indirizzabili e quindi
non compaiono in alcun documento OpenAPI. Il progetto espone soltanto **9 route handler
REST** (contro una media di 35 operazioni sugli altri progetti).

Poiché la base di valutazione è `openapi_documentation`, il metodo **non può
strutturalmente osservare** la maggior parte dell'implementazione di team04.

**Implicazione:** si tratta di un limite di validità dell'approccio, non di un errore di
misura. Va riportato esplicitamente e, nelle statistiche aggregate, team04 andrebbe
trattato separatamente o escluso con motivazione. Più in generale: *il metodo assume che
la superficie REST sia rappresentativa dell'applicazione*, assunzione che decade per le
architetture full-stack con logica lato server non esposta via HTTP.

---

## 4. Caveat 2 — la provenienza degli spec è un confondente

Alla data della run, **16 dei 18 file `swagger.yaml` non erano stati scritti dai team**:
sono stati prodotti il 21 luglio 2026 con l'assistenza di un LLM (Claude). Nel dettaglio:

- **8 ricostruiti da zero** leggendo i router del codice sorgente, per i progetti che non
  avevano alcuna documentazione API: team02, 04, 06, 08, 11, 12, 15, 17.
- **8 consolidati** a partire dalla documentazione esistente dei team (file YAML/JSON
  sparsi, o annotazioni `swagger-jsdoc` distribuite nel codice), riportata in un unico
  file standard senza reinterpretarne il contenuto: team03, 05, 07, 10, 13, 14, 16, 18.
- **2 originali intatti**: team01 e team09.

Nella tabella di §2 la colonna *Spec* distingue i soli **ricostruiti da zero** (8) da
tutti gli altri (10), che conservano il contenuto informativo prodotto dai team.

### Effetto misurato

| Gruppo | n | F1 | Precision | Recall |
| --- | ---: | ---: | ---: | ---: |
| Spec di provenienza team | 10 | 0.819 | 0.846 | 0.817 |
| Spec ricostruiti | 8 | 0.719 | **0.849** | 0.685 |
| *Differenza* | | *+0.100* | *−0.003* | *+0.132* |

Escludendo l'outlier team04:

| Gruppo | n | F1 | Precision | Recall |
| --- | ---: | ---: | ---: | ---: |
| Spec di provenienza team | 10 | 0.819 | 0.846 | 0.817 |
| Spec ricostruiti | 7 | 0.778 | 0.827 | 0.757 |
| *Differenza* | | *+0.041* | *+0.019* | *+0.060* |

### Interpretazione

Il dato diagnostico è che **la precision è praticamente identica** fra i due gruppi
(0.846 vs 0.849): gli spec ricostruiti descrivono gli endpoint in modo altrettanto
accurato. L'intero divario si concentra sul **recall** (0.817 vs 0.685).

La spiegazione più plausibile è che la ricostruzione automatica abbia dato priorità alla
*completezza degli endpoint* (metodo, path, parametri, codici di stato) producendo però
`summary` e `description` più scarni, mentre è proprio da quel testo in prosa che
l'agente *matcher* trae il segnale semantico per collegare un requisito a un'operazione.
Meno prosa descrittiva → meno appigli per il match → più falsi negativi.

Escludendo team04 il divario si riduce sensibilmente (F1 +0.041), il che indica che gran
parte della differenza grezza è attribuibile a quel singolo caso e non a un effetto
sistematico forte.

**Implicazione:** il confronto *fra progetti* va interpretato con cautela, perché la
qualità della documentazione di input non è omogenea per provenienza. Una verifica utile
sarebbe arricchire le descrizioni di 2–3 spec ricostruiti e rimisurare, per quantificare
direttamente il contributo della prosa descrittiva al recall.

---

## 5. Note metodologiche

### Consolidamento
I 18 progetti provengono da circa 20 esecuzioni distinte, perché i tentativi in batch si
interrompevano ripetutamente (crash del motore di inferenza, risposte JSON malformate o
troncate). La cartella consolidata è stata prodotta da `scripts/consolidate_runs.py`, che
seleziona per ciascun progetto l'esecuzione completa più recente. La corrispondenza
progetto → run di origine è tracciata in `provenance.json` e riportata nell'ultima colonna
di §2.

### Validità del confronto fra progetti
Ogni esecuzione rigenera la propria analisi dei requisiti, quindi in linea di principio i
progetti potevano essere stati valutati contro requisiti diversi. È stato verificato che
**tutte e 21 le esecuzioni hanno prodotto requisiti byte-identici** (stessi 30 ID PT01–PT30
e stessi testi): l'agente `requirements_analyst` è risultato deterministico a temperatura
0.1 sugli stessi documenti di input. Il confronto fra progetti è quindi legittimo.

Lo script di consolidamento verifica automaticamente questa condizione e si interrompe se
le esecuzioni non concordano, oltre a rifiutare gli artefatti prodotti in modalità
*dry-run* (fixture mock) che altrimenti apparirebbero come risultati validi.

### Prestazioni
Il tempo di esecuzione scala con il **numero di operazioni OpenAPI**, non con il numero di
progetti: circa **12,5 secondi per operazione** su questa configurazione hardware. Le 626
operazioni complessive corrispondono a circa 2 ore di elaborazione netta.

### Limite noto, non ancora affrontato
Gli spec più grandi (≈50 operazioni) sono al limite della finestra di contesto da 16k:
team10 (49 operazioni) e team13 (51) hanno fallito ripetutamente per troncamento della
risposta nella fase `api_understanding`, mentre team16 (50) è passato. È una soglia in cui
l'esito dipende dalla varianza del modello. Entrambi sono stati recuperati con
riesecuzioni singole; una soluzione strutturale richiede una finestra di contesto più
ampia o la compattazione del prompt di `api_understanding` (che però, se introdotta,
andrebbe applicata rilanciando tutti e 18 i progetti per non comprometterne la
comparabilità).

---

## 6. Artefatti prodotti

Nella cartella `data/runs/20260721T235416Z-consolidated`:

| File | Contenuto |
| --- | --- |
| `requirement_coverage_matrix.csv` | Matrice 30 requisiti × 18 progetti con lo stato per cella |
| `requirement_coverage_matrix.json` | Stessa matrice con i dettagli dei match |
| `coverage_evaluation.{json,csv,md}` | Confronto con la ground truth e metriche per progetto |
| `summary.md` | Riepilogo generato dalla pipeline |
| `provenance.json` | Mappatura progetto → run di origine |
| `projects/<team>/` | Artefatti per progetto (analisi API, copertura, strategia di test, piano) |
