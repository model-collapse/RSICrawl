#!/usr/bin/env bash
# Reproduce the RSICrawl benchmark (Track S, synthetic sites). Takes several hours; LLM spend about $15-20.
# Requires: Lightpanda (RSICRAWL_LIGHTPANDA), Claude access (ANTHROPIC_API_KEY, or AWS Bedrock with
# RSICRAWL_LLM_PROVIDER=bedrock), docker (skill sandbox). Ports 18001-18044 and 18999 must be free.
set -euo pipefail
cd "$(dirname "$0")/.."
export LIGHTPANDA_DISABLE_TELEMETRY=true
OUT=${OUT:-runs}; mkdir -p "$OUT/p3"
F=$OUT/forge11
python -m siteforge.generate --seed 11 --out $F --sites-per-combo 2 --items 100
run() { local out=$1; shift; python -m bench.run --forge $F --split test --rate 2 --spend-cap 15 --out $OUT/$out "$@"; python -m bench.score $OUT/$out > /dev/null; }

# Gyms (offline evidence per URL pattern) and generation-0 analyzer solve rates
for s in train val test; do
  python -m bench.gym --forge $F --split $s --out $OUT/g11_$s.json
  python -m bench.gymscore $OUT/g11_$s.json --out $OUT/p3/gen0_$s.json
done

# P1: cold stream on the test split, all systems
for sys in B0 B2 B3 B4 SYS; do run p1_$sys --system $sys; done

# P2: learn on the train stream, then the test stream with that memory (recipes dropped)
python -m bench.run --forge $F --split train --rate 2 --spend-cap 15 --out $OUT/p2_train --system SYS
run p2_warm_gen0 --system SYS --memory-from $OUT/p2_train/memory

# P3: two method generations (Improve episodes on the train gym, then train+val), scored on held-out gyms
cp -r $OUT/p2_train/memory $OUT/p3/mem_gen1
python -m bench.improve --memory $OUT/p3/mem_gen1 --gym $OUT/g11_train.json --out $OUT/p3/improve_gen1.json
for s in train val test; do python -m bench.gymscore $OUT/g11_$s.json --memory $OUT/p3/mem_gen1 --out $OUT/p3/gen1_$s.json; done
python -c "import json; a=json.load(open('$OUT/g11_train.json')); a.update(json.load(open('$OUT/g11_val.json'))); json.dump(a, open('$OUT/g11_trainval.json','w'))"
cp -r $OUT/p3/mem_gen1 $OUT/p3/mem_gen2
python -m bench.improve --memory $OUT/p3/mem_gen2 --gym $OUT/g11_trainval.json --out $OUT/p3/improve_gen2.json
for s in train val test; do python -m bench.gymscore $OUT/g11_$s.json --memory $OUT/p3/mem_gen2 --out $OUT/p3/gen2_$s.json; done

# Final system, light and heavy pages (SITEFORGE sub-resources), with drift (P4); speed criteria vs B0
run light_B0 --system B0
run light_warm --system SYS --memory-from $OUT/p3/mem_gen2
run light_drift --system SYS --memory-from $OUT/p3/mem_gen2 --drift
run heavy_B0 --system B0 --subres 6
run heavy_cold --system SYS --subres 6
run heavy_warm --system SYS --subres 6 --memory-from $OUT/p3/mem_gen2
run heavy_drift --system SYS --subres 6 --memory-from $OUT/p3/mem_gen2 --drift
for r in light_warm light_drift; do python -m bench.speed $OUT/$r $OUT/light_B0; done
for r in heavy_cold heavy_warm heavy_drift; do python -m bench.speed $OUT/$r $OUT/heavy_B0; done

# Confirmation on fresh sites (seed 23)
F2=$OUT/forge23; python -m siteforge.generate --seed 23 --out $F2 --sites-per-combo 2 --items 100
python -m bench.run --forge $F2 --split test --rate 2 --subres 6 --system B0 --out $OUT/f23_B0
python -m bench.run --forge $F2 --split test --rate 2 --subres 6 --system SYS --memory-from $OUT/p3/mem_gen2 --out $OUT/f23_warm
python -m bench.score $OUT/f23_warm > /dev/null; python -m bench.speed $OUT/f23_warm $OUT/f23_B0
