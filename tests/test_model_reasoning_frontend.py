#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_URL = (ROOT / "web" / "src" / "utils" / "modelReasoning.js").as_uri()


class ModelReasoningFrontendTests(unittest.TestCase):
    def test_known_model_lists_only_its_options_and_unknown_lists_common_options(self):
        scenario = f"""
          import {{
            commonReasoningEfforts,
            reasoningCapabilityForModel,
          }} from {json.dumps(MODULE_URL)};
          const capabilities = [
            {{key: 'qwen', model_pattern: '^qwen-3$', accepted_efforts: ['none', 'low', 'high']}},
            {{key: 'fixed', model_pattern: '^fixed$', accepted_efforts: []}},
            {{key: 'glm', model_pattern: '^glm-5$', accepted_efforts: ['disabled', 'enabled']}},
          ];
          const qwen = reasoningCapabilityForModel(' QWEN-3 ', capabilities);
          const fixed = reasoningCapabilityForModel('fixed', capabilities);
          const unknown = reasoningCapabilityForModel('gateway-custom', capabilities);
          console.log(JSON.stringify({{
            qwenKey: qwen?.key,
            qwenOptions: qwen?.accepted_efforts,
            fixedOptions: fixed?.accepted_efforts,
            unknown,
            common: commonReasoningEfforts(capabilities),
          }}));
        """
        result = subprocess.run(
            ["node", "--input-type=module", "-e", scenario],
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(result.stdout)

        self.assertEqual(payload["qwenKey"], "qwen")
        self.assertEqual(payload["qwenOptions"], ["none", "low", "high"])
        self.assertEqual(payload["fixedOptions"], [])
        self.assertIsNone(payload["unknown"])
        self.assertEqual(
            payload["common"],
            ["none", "low", "high", "disabled", "enabled"],
        )


if __name__ == "__main__":
    unittest.main()
