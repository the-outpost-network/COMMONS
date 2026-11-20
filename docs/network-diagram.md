# Network components and services in use now

This diagram reflects the current stack defined in `docker-compose.yml` and the repo documentation.

```mermaid
flowchart LR
    subgraph Client[Clients]
        Browser[Browser / admin UI]
        Element[Element / Matrix client]
    end

    subgraph Host[One box / Docker Compose]
        subgraph Web[Public edge]
            Caddy[Caddy\nTLS terminator\nonly published port: 443]
        end

        subgraph Internal[Internal Docker network]
            App[COMMONS app\nFastAPI + SSE + web UI]
            Matrix[Synapse\nMatrix homeserver]
            MAS[Matrix Authentication Service\nMAS / auth / token issuer]
            Postgres[PostgreSQL 17\nCOMMONS + Synapse + MAS DBs]
            LLM[llama.cpp\nSmolLM3-3B model server]
            Bot[Matrix bot\nmautrix-python relay]
            Egress[Allowlist egress proxy\nSMHI + Matrix push only]
        end
    end

    subgraph External[Allowed outbound services]
        MatrixPush[Matrix push gateway]
        SMHI[SMHI weather API]
    end

    Browser --> Caddy
    Element --> Caddy
    Caddy --> App
    Caddy --> Matrix
    Caddy --> MAS

    App --> Postgres
    Matrix --> Postgres
    MAS --> Postgres

    App --> LLM
    App --> Matrix
    App --> MAS

    Bot --> Matrix
    Bot --> App

    Matrix -. "HTTPS proxy" .-> Egress
    App -. "weather via proxy" .-> Egress
    Egress --> MatrixPush
    Egress --> SMHI

    classDef service fill:#dfeaf7,stroke:#2c5282,color:#111827;
    class Caddy,App,Matrix,MAS,Postgres,LLM,Bot,Egress service;
```

## Notes

- Only Caddy exposes a host port; all other services stay on the Docker `internal` network.
- `bot` and `llm` are not directly reachable from the LAN.
- `egress` is the only outbound route for approved traffic (Matrix push and SMHI weather). Everything else stays internal.
- `app` talks to `matrix`, `mas`, `postgres`, and `llm` over Docker service names such as `http://matrix:8008`, `http://mas:8080`, and `http://llm:8080`.
- Secrets and runtime credentials are mounted from `./secrets` and never passed as plain environment variables.
