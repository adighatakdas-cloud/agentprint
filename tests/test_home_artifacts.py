"""Home and project origin of artifacts (AP008 checks home data, not shared project config)."""

from agentprint.agents import ALL_AGENTS
from agentprint.agents.base import HomeArtifact


def test_home_discovery_tags_artifacts_with_the_home_folder(fake_home, agents_home, more_home):
    for home in (fake_home, agents_home, more_home):
        found = [a for agent in ALL_AGENTS for a in agent.discover_home(home)]
        assert found
        assert all(isinstance(a, HomeArtifact) and a.home == home for a in found)


def test_project_discovery_is_not_home_data(fake_project, agents_project, more_project):
    for root in (fake_project, agents_project, more_project):
        found = [a for agent in ALL_AGENTS for a in agent.discover_project(root)]
        assert found
        assert not any(isinstance(a, HomeArtifact) for a in found)
