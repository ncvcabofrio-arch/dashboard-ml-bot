name: Mutirao historico (por periodo)

on:
  workflow_dispatch:
    inputs:
      data_ini:
        description: 'Data inicial (AAAA-MM-DD)'
        required: true
        default: '2026-01-01'
      data_fim:
        description: 'Data final (AAAA-MM-DD) — deixe vazio para "hoje"'
        required: false
        default: ''
      modo:
        description: 'tudo = puxa pedidos + enriquece | enriquecer = só frete/repasse/estado (não re-puxa)'
        required: false
        default: 'tudo'
        type: choice
        options:
          - tudo
          - enriquecer

concurrency:
  group: ml-puxador
  cancel-in-progress: false

jobs:
  historico:
    runs-on: ubuntu-latest
    timeout-minutes: 350
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - name: Instalar dependencias
        run: pip install requests supabase
      - name: Rodar mutirao historico
        env:
          ML_CLIENT_ID:     ${{ secrets.ML_CLIENT_ID }}
          ML_CLIENT_SECRET: ${{ secrets.ML_CLIENT_SECRET }}
          ML_REFRESH_TOKEN: ${{ secrets.ML_REFRESH_TOKEN }}
          SUPABASE_URL:     ${{ secrets.SUPABASE_URL }}
          SUPABASE_KEY:     ${{ secrets.SUPABASE_KEY }}
          DATA_INI:         ${{ inputs.data_ini }}
          DATA_FIM:         ${{ inputs.data_fim }}
          MODO:             ${{ inputs.modo }}
        run: python backfill_historico.py
