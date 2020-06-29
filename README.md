# memgrep_volatility_plugin
Given a string or regular expression, the plugin should print all its occurrences and for each one tells where it is located in the memory dump (physical and virtual address, allocated or unallocated block, kernel vs process memory, heap vs stack vs data sections, … ) This is somehow similar to yarascan or running strings+memap plugin, but should give much more information.
