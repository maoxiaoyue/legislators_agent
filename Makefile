# Generate Go code from .proto files (buf drives local protoc-gen-go / protoc-gen-go-grpc; no protoc needed)
.PHONY: proto
proto:
	buf generate

# Run the gRPC server
.PHONY: run
run:
	go run .

# Download MOJ open data (法律 + 命令), build data/laws/*.json and the vector index
.PHONY: ingest
ingest:
	go run . ingest

# Rebuild the vector index from data/laws/*.json without downloading
.PHONY: reindex
reindex:
	go run . ingest -reindex -reset

# Test
.PHONY: test
test:
	go test ./...

# Build
.PHONY: build
build:
	go build -o bin/legagent .

