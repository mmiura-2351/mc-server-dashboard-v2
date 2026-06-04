package containerdriver

import (
	"fmt"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/adapters/javaruntime"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
)

// ImageSelector resolves a Minecraft version to a base container image. It
// mirrors the JavaRuntimeSelector concept (CONFIGURATION.md worker section):
// driver.container.images maps a Java major version to an image ref, and the
// version→major bracket logic is shared with javaruntime so a server runs on the
// same Java major whether it executes as a host process or in a container.
type ImageSelector struct {
	// images maps a Java major version to the base image ref providing that JRE.
	images map[int]string
}

// NewImageSelector builds an ImageSelector over the configured Java-major→image
// map (driver.container.images).
func NewImageSelector(images map[int]string) *ImageSelector {
	return &ImageSelector{images: images}
}

// Select returns the base image for mcVersion. It picks the required Java major
// from the shared mapping, then resolves the configured image (preferring the
// most-preferred major, falling back like javaruntime does). It returns
// execution.ErrNoRuntime when no configured image satisfies the version, or a
// parse error when mcVersion is unparseable.
func (s *ImageSelector) Select(mcVersion string) (string, error) {
	majors, err := javaruntime.MajorsFor(mcVersion)
	if err != nil {
		return "", err
	}
	for _, major := range majors {
		if image, ok := s.images[major]; ok {
			return image, nil
		}
	}
	return "", fmt.Errorf("%w: Minecraft %s needs Java %v, no container image configured", execution.ErrNoRuntime, mcVersion, majors)
}
