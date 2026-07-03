// Deliberately a separate, standalone module (stdlib only): stub-geyser is a
// throwaway test fixture built only via its Dockerfile (`docker build`), never
// part of the worker module's own build/vet/lint sweep -- mirrors the sibling
// worker/test/e2e/stub/ image, which ships a shell script with no Go module at
// all. See main.go's doc comment.
module github.com/mmiura-2351/mc-server-dashboard-v2/worker/test/e2e/stub-geyser

go 1.26
