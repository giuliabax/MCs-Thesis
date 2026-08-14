# Risultati delle tre esecuzioni ripetute sui 18 progetti

**Run analizzate:** `20260807T151226Z`, `20260807T165706Z`, `20260807T190701Z`
**Batch di provenienza:** `data/runs/20260807T151225Z-repeated`, `data/runs/20260807T190700Z-repeated`
**Data:** 7 agosto 2026
**Modello:** `qwen/qwen3.5-9b` (Q4_K_M) in locale via LM Studio
**Hardware:** MSI Vector 16 — Intel Ultra 9, 32 GB RAM, RTX 5080 Laptop 16 GB VRAM
**Ground truth:** `data/ground_truth/participium_implemented_stories.yaml`
**Base di valutazione:** `openapi_documentation` (30 requisiti PT01–PT30 × 18 progetti = 540 celle)
**Baseline di confronto:** `20260721T235416Z-consolidated` (§ [run-results-2026-07-21](run-results-2026-07-21.md))

Rispetto alla baseline di luglio cambia l'hardware (da RTX 3070 8 GB a RTX 5080 Laptop
16 GB) e la configurazione che quell'hardware rendeva possibile. Il **modello è lo
stesso** e **gli input sono gli stessi**: gli spec OpenAPI sono stati ricostruiti dagli
artefatti della run di luglio con verifica di round-trip campo per campo (§7), quindi il
matcher riceve esattamente le stesse 626 operazioni.

---

## 1. Metriche aggregate (n = 3)

| Metrica | Media ± dev. std. | Luglio (n = 1) | Δ |
| --- | ---: | ---: | ---: |
| Macro precision | 0.837 ± 0.014 | 0.847 | −0.010 |
| Macro recall | 0.767 ± 0.019 | 0.758 | **+0.009** |
| **Macro F1** | **0.773 ± 0.010** | 0.775 | −0.002 |
| Micro precision | 0.823 ± 0.011 | 0.837 | −0.014 |
| Micro recall | 0.758 ± 0.022 | 0.748 | **+0.010** |
| **Micro F1** | **0.789 ± 0.011** | 0.790 | −0.001 |

La differenza di F1 rispetto a luglio (0.002 macro, 0.001 micro) è **un quinto della
deviazione standard fra run**: il metodo si comporta come prima. Il recall è
marginalmente superiore, la precision marginalmente inferiore — il sistema resta
conservativo, ma un po' meno.

### Per singola run

| Run | Macro F1 | Micro F1 | Macro P | Macro R | TP | FP | FN | TN |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `20260807T151226Z` | 0.781 | 0.798 | 0.830 | 0.787 | 273 | 62 | 76 | 129 |
| `20260807T165706Z` | 0.763 | 0.777 | 0.827 | 0.749 | 258 | 57 | 91 | 134 |
| `20260807T190701Z` | 0.777 | 0.792 | 0.853 | 0.765 | 263 | 52 | 86 | 139 |

Le tre run si dispongono lungo un asse precision/recall: la prima è la più permissiva
(recall più alto, più falsi positivi), la terza la più prudente. L'F1 le colloca a meno
di 0.02 l'una dall'altra.

---

## 2. Risultati per progetto (n = 3)

| Progetto | F1 medio | Recall medio | F1 luglio | Δ |
| --- | ---: | ---: | ---: | ---: |
| team01 | 0.777 ± 0.058 | 0.759 ± 0.128 | 0.842 | −0.065 |
| team02 | 0.753 ± 0.005 | 0.972 ± 0.048 | 0.800 | −0.047 |
| team03 | 0.812 ± 0.020 | 0.877 ± 0.110 | 0.829 | −0.017 |
| team04 | 0.263 ± 0.039 | 0.152 ± 0.026 | 0.308 | −0.045 |
| team05 | 0.747 ± 0.037 | 0.635 ± 0.055 | 0.667 | +0.080 |
| team06 | 0.825 ± 0.057 | 0.807 ± 0.132 | 0.765 | +0.060 |
| team07 | 0.686 ± 0.041 | 0.540 ± 0.055 | 0.667 | +0.019 |
| team08 | 0.795 ± 0.052 | 0.742 ± 0.095 | 0.732 | +0.063 |
| team09 | 0.750 ± 0.007 | 0.778 ± 0.056 | 0.778 | −0.028 |
| team10 | 0.887 ± 0.034 | 0.965 ± 0.061 | 0.900 | −0.013 |
| team11 | 0.741 ± 0.032 | 0.702 ± 0.030 | 0.727 | +0.014 |
| team12 | 0.764 ± 0.087 | 0.789 ± 0.158 | 0.737 | +0.027 |
| team13 | 0.896 ± 0.023 | 0.952 ± 0.000 | 0.864 | +0.032 |
| team14 | 0.858 ± 0.022 | 0.882 ± 0.059 | 0.833 | +0.025 |
| team15 | 0.768 ± 0.038 | 0.714 ± 0.082 | 0.743 | +0.025 |
| team16 | 0.921 ± 0.040 | 0.923 ± 0.102 | 0.963 | −0.042 |
| team17 | 0.875 ± 0.060 | 0.852 ± 0.085 | 0.944 | −0.069 |
| team18 | 0.805 ± 0.038 | 0.765 ± 0.000 | 0.848 | −0.043 |

team04 resta l'outlier strutturale già discusso nel §3 di luglio: applicazione Next.js
con la logica nelle Server Actions, 9 sole route REST, quindi non osservabile per
costruzione da una base di valutazione `openapi_documentation`.

### La varianza per progetto è l'osservazione principale

L'aggregato è stabile (dev. std. 0.010 sul macro F1) mentre i singoli progetti oscillano
di un ordine di grandezza in più: team12 F1 ± 0.087, team01 recall ± 0.128, team12 recall
± 0.158. Gli errori per progetto si compensano nella media.

**Implicazione metodologica:** i confronti *aggregati* fra configurazioni sono
affidabili; i confronti *su un singolo progetto* fra due run singole non lo sono. Un
effetto inferiore a ≈0.05 di F1 su un progetto non è distinguibile dal rumore senza
repliche. Retroattivamente, questo significa che anche i valori per progetto della
tabella di luglio, prodotti da un'unica esecuzione, andavano letti con più cautela di
quanto la loro precisione apparente suggerisse.

---

## 3. Esperimento: `max_tokens` come budget di ragionamento

È il risultato non ovvio di questa sessione, ed è emerso da un'indagine su un'anomalia.

### L'anomalia

Una prima esecuzione sul nuovo hardware (`20260807T103836Z`) aveva prodotto macro F1
0.765 con tre progetti in netto calo rispetto a luglio: team16 (0.963 → 0.833), team17
(0.944 → 0.833), team18 (0.848 → 0.714). Gli input erano identici, verificati.

L'esame delle decisioni per requisito ha mostrato una direzione comune: il modello si
ritirava dalle asserzioni positive. team16 spostava 6 requisiti da
`implemented`/`partially_implemented` a `not_assessable`; team18 degradava 5
`partially_implemented` in `not_implemented`. Il recall crollava, la precision reggeva.

Sullo stesso requisito, le due run motivavano in modo diverso:

| | Luglio | Agosto (`max_tokens` 20000) |
| --- | --- | --- |
| **PT04** | `implemented`, match su `POST /reports` — *«accetta dati di posizione per la geolocalizzazione»* | `not_assessable`, nessun match — *«la selezione della posizione è interazione UI/mappa; OpenAPI non espone endpoint di geolocalizzazione»* |
| **PT14** | `implemented`, match su `GET /faqs` — *«fornisce comandi di aiuto e informazioni di assistenza»* | `not_assessable`, nessun match — *«i comandi del bot Telegram sono feature di integrazione esterna»* |

Non un errore di parsing né un'omissione: un **criterio interpretativo più severo**. La
ground truth, però, premia il matching inferenziale più permissivo.

### L'ipotesi e la sua verifica

`requirement_api_matcher` è l'unico agente con la fase di ragionamento attiva. Fra luglio
e la run anomala `max_tokens` era passato da 9000 a 20000: per un agente che ragiona quel
parametro non è solo un tetto di lunghezza, è **quanto spazio ha il modello per
deliberare prima di rispondere**.

Ablazione su team16 (50 operazioni, 30 requisiti), stessa chiamata ripetuta:

| Cella | `not_assessable` | positivi | `partially_implemented` | completion tokens |
| --- | ---: | ---: | ---: | ---: |
| 9000, run 1 | **0** | 25 | **4** | 4 590 |
| 9000, run 2 | **0** | 27 | **8** | 4 977 |
| 20000, run 1 | 2 | 23 | **0** | 11 108 |
| 20000, run 2 | 1 | 27 | **0** | 4 861 |
| *run completa @20000* | *7* | *22* | *—* | *—* |

Il segnale più netto non è `not_assessable` ma **`partially_implemented`**: a 9000 il
modello usa l'etichetta intermedia, a 20000 non la usa mai. Con più spazio per deliberare
si polarizza — o afferma, o rifiuta. E poiché `partially_implemented` conta come
predizione positiva, abbandonarla costa recall direttamente. È lo stesso meccanismo sotto
i due sintomi osservati (team16 verso `not_assessable`, team18 verso `not_implemented`).

### Conferma sulle tre run

Riportato il matcher a 9000 tramite override per agente:

| Progetto | @20000 | @9000 (n = 3) | Luglio |
| --- | ---: | ---: | ---: |
| team16 | 0.833 | **0.921 ± 0.040** | 0.963 |
| team17 | 0.833 | **0.875 ± 0.060** | 0.944 |
| team18 | 0.714 | **0.805 ± 0.038** | 0.848 |

Conteggi di `partially_implemented` nelle tre run: team16 `9|10|4`, team17 `3|4|4`,
team18 `2|6|4` — mai zero, contro lo zero misurato a 20000.

### Limiti dell'esperimento

L'ablazione ha **n = 2 per braccio**. Le 6 osservazioni disponibili sono coerenti in
direzione (a 9000 `not_assessable` è sempre 0; a 20000 non è mai 0), ma il valore 7 della
run completa non è stato riprodotto — le repliche danno 1 e 2. Il crollo osservato su
team16 è quindi budget **più** un'estrazione sfavorevole, non budget da solo. La
grandezza dell'effetto resta non quantificata.

---

## 4. Affidabilità della pipeline

### Ritentativi per agente

| Run | `requirement_api_matcher` | `test_strategy_planner` | `api_understanding` | Totale |
| --- | ---: | ---: | ---: | ---: |
| `20260807T151226Z` | 14 | 8 | 1 | 23 |
| `20260807T165706Z` | 15 | 3 | 0 | 18 |
| `20260807T190701Z` | 15 | 4 | 0 | 19 |

**Il matcher è il punto debole strutturale**: da solo vale circa i tre quarti di tutte le
riparazioni, in modo stabile fra le run. Il dato è indipendente da `max_tokens` —
nell'ablazione ha richiesto la riparazione dello schema in 3 celle su 4 a entrambi i
budget.

`api_understanding`, che a luglio faceva fallire ripetutamente i progetti più grandi per
troncamento della risposta a 16k, ora è quasi silente: 1 ritentativo in tre run. team10
(49 operazioni), team13 (51) e team16 (50) sono passati in tutte e tre le esecuzioni
senza riesecuzioni manuali.

### Fallimenti di run

Con la configurazione finale, 3 esecuzioni su 4 tentate sono arrivate in fondo. L'unica
fallita si è interrotta al progetto 11 con
`Test Strategy Planner still failed semantic quality checks after one corrective call:
missing required test types: negative`. Causa: il modello aveva prodotto i propri test
negativi su endpoint inesistenti; la normalizzazione li ha scartati e la strategia è
rimasta priva di un tipo obbligatorio. La correzione descritta in §6 chiude questo caso,
ma è successiva alle tre run qui riportate.

---

## 5. Correzioni applicate alla pipeline

Tutte introdotte in questa sessione, tutte coperte da test di regressione.

| Modifica | Motivazione | Effetto sulle metriche di copertura |
| --- | --- | --- |
| `llm.overrides[agent].max_tokens` | Separare il budget del matcher da quello del planner: hanno bisogni opposti | **Sì** — è l'oggetto del §3 |
| Ricostruzione della `rationale` omessa | Campo obbligatorio che il modello salta su una minoranza di match, facendo fallire l'intero progetto | No — nessuna metrica lo legge |
| `batch_strategy_planner: false` | Con una finestra ampia il planner pianifica in una chiamata sola, contro l'obiettivo globale anziché per lotti ciechi | No — la copertura precede il planner |
| Scarto degli item su operazioni assenti | Un item su un endpoint non documentato è intestabile, non solo non verificato | No |
| Sintesi dei tipi di test mancanti | Solo come ultima risorsa, dopo che la chiamata correttiva ha già fallito | No |

Le `rationale` ricostruite sono marcate con il prefisso `Synthesized from evidence` e
restano distinguibili negli artefatti: 17, 33 e 34 su 540 match nelle tre run
(team09, team11, team12). **Non vanno citate come valutazioni del modello.**

Endpoint inventati e scartati: 10 nel primo batch, 5 nel secondo.

---

## 6. Caveat

**Provenienza degli spec.** Il confondente descritto nel §4 di luglio resta valido e
immutato: 16 dei 18 `swagger.yaml` non sono stati scritti dai team. Gli spec usati qui
sono ricostruiti per round-trip dagli artefatti di luglio, quindi riproducono *anche* la
povertà descrittiva degli 8 ricostruiti da zero, che è la causa più probabile del loro
recall inferiore.

**Impostazioni di caricamento del modello non tracciate.** `config.resolved.yaml`
registra la configurazione della pipeline, non i parametri con cui LM Studio ha caricato
il modello. La finestra di contesto (portata da 16384 a 65536), la quantizzazione della
KV cache e il seed restano fuori banda: vanno annotati manualmente per la riproducibilità.

**Il seed non è fissato.** L'inferenza usa un seed casuale a temperatura 0.1. La
dispersione riportata include quindi la variabilità di campionamento, che è la quantità
che si intendeva misurare. Un seed fisso non garantirebbe comunque la riproducibilità
bit-a-bit su GPU.

**n = 3.** Le deviazioni standard sono stimate su tre osservazioni: indicative
dell'ordine di grandezza, non solide per un test statistico.

**team04.** Da trattare separatamente o escludere con motivazione nelle statistiche
aggregate, per le ragioni strutturali del §3 di luglio.

---

## 7. Ricostruzione degli spec OpenAPI

I 18 `swagger.yaml` sono input locali non versionati e non erano presenti sulla nuova
macchina. Sono stati ricostruiti da `openapi_operations.json` della run consolidata di
luglio tramite `scripts/rebuild_swagger_from_run.py`, che verifica il round-trip: ogni
file ricostruito, ricaricato con `OpenAPILoader`, produce operazioni identiche campo per
campo a quelle che la run di luglio aveva consumato.

17 ricostruiti, 1 lasciato intatto (team09, spec originale del team, che concorda a sua
volta con la baseline). Totale verificato: **626 operazioni**, come a luglio.

La ricostruzione è fedele a ciò che la pipeline legge, non a ciò che leggerebbe una
persona: componenti, `servers` e schemi che il loader scarta non sono stati ripristinati.
La scelta preserva la comparabilità con la baseline; rileggere i sorgenti dei progetti
avrebbe prodotto documenti diversi e reintrodotto il confondente del §4 in forma nuova.

---

## 8. Artefatti

| Percorso | Contenuto |
| --- | --- |
| `data/runs/20260807T151225Z-repeated/aggregate.{json,csv,md}` | Batch di 3 run (2 riuscite), media ± dev. std. |
| `data/runs/20260807T190700Z-repeated/aggregate.{json,csv,md}` | Terza run |
| `data/runs/<run_id>/coverage_evaluation.{json,csv,md}` | Confronto con la ground truth per run |
| `data/runs/<run_id>/requirement_coverage_matrix.{csv,json}` | Matrice 30 requisiti × 18 progetti |
| `data/runs/<run_id>/projects/<team>/` | Artefatti per progetto |
| `scripts/repeated_runs.py` | Esecuzioni ripetute con aggregazione |
| `scripts/rebuild_swagger_from_run.py` | Ricostruzione degli spec con verifica di round-trip |

### Run selezionata per le fasi successive

`20260807T151226Z`, scelta come F1 di copertura più alto (macro 0.781, micro 0.798). La
selezione usa deliberatamente la ground truth e va dichiarata come tale: il valore
riportato per la qualità della pianificazione resta **0.773 ± 0.010 su n = 3**, non
quello della run selezionata.

È anche la run più permissiva delle tre: 62 falsi positivi contro i 52 della terza. La
generazione produrrà quindi test per alcuni requisiti che il progetto non implementa; i
loro fallimenti andranno attribuiti alla copertura a monte, non ai test generati.
