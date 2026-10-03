"""Pluggable providers for the script LLM and for text-to-speech.

How to add a provider
---------------------
1. Create a module in ``providers/llm/`` (or ``providers/tts/``), e.g. ``my_vendor.py``.
   Modules in those packages are imported automatically; third-party code only needs to import
   its module before the config is loaded.
2. Define an options model deriving from `ProviderOptions` (providers.registry)
   with a ``provider: Literal["my_vendor"] = "my_vendor"`` field and the provider's settings.
   These are the keys users write under ``llm:`` / ``tts:`` in ``config.yaml`` (or
   ``C2P_LLM__...`` environment variables).
3. Write a factory ``(options, secrets) -> provider`` and register it::

       @register_llm("my_vendor", options=MyVendorOptions)
       def build(options: MyVendorOptions, secrets: Secrets) -> LLMProvider:
           return MyVendorLLM(options, api_key=secrets.my_vendor_key)

   The provider object only has to satisfy the ``LLMProvider`` / ``TTSProvider`` protocol.
   Credentials come from :class:`~content2podcast.config.Secrets`, never from the options model.

Nothing else needs to change: config validation, the "unknown provider" message and
``build_llm`` / ``build_tts`` pick the new provider up through the registry.
``providers/llm/fake.py`` and ``providers/tts/fake.py`` are complete minimal examples.
"""
