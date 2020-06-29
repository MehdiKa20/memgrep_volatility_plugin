# memgrep_volatility_plugin
Given a string or regular expression, this plugin prints all its occurrences and for each one tells where it is located in the memory dump  (virtual address, allocated or unallocated block, kernel vs process memory, heap vs stack vs data sections)
This is somehow similar to running strings+memap plugin.
