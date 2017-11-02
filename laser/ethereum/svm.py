from laser.ethereum import helper
from ethereum import utils
from enum import Enum
from z3 import *
import copy
import logging
import sha3


TT256 = 2 ** 256
TT256M1 = 2 ** 256 - 1
TT255 = 2 ** 255

MAX_DEPTH = 16

gbl_next_uid = 0


class SVMError(Exception):
    pass


class JumpType(Enum):
    CONDITIONAL = 1
    UNCONDITIONAL = 2
    CALL = 3
    RETURN = 4


class CalldataType(Enum):
    CONCRETE = 1
    SYMBOLIC = 2


class State(): 

    def __init__(self, gas=1000000):
        self.storage = {}
        self.memory = []
        self.stack = []
        self.last_returned = []
        self.gas = gas
        self.pc = 0

    def as_dict(self):

        return {'memory': self.memory, 'stack': self.stack, 'storage': self.storage, 'pc': self.pc, 'gas': self.gas}


    def mem_extend(self, start, sz):

        logging.info("mem_externd: " + str(start) + ", " + str(sz))

        if sz and start + sz > len(self.memory):

            n_append = start + sz - len(self.memory)

            while n_append > 0:
                self.memory.append(0)
                n_append -= 1

            # Deduct gas.. not yet implemented


class Context(): 

    def __init__(
        self,
        module,
        calldata = [],
        callvalue = BitVec("callvalue", 256),
        caller = BitVec("caller", 256),
        origin = BitVec("origin", 256),
        address = BitVec("address", 256),
        calldata_type = CalldataType.SYMBOLIC
        ):

        self.module = module
        self.calldata = calldata # bytelist
        self.callvalue = callvalue
        self.caller = caller
        self.origin = origin
        self.address = address
        self.calldata_type = calldata_type


class Node:

    def __init__(self, module_name, start_addr=0, constraints = []):
        self.module_name = module_name
        self.start_addr = start_addr
        self.instruction_list = []
        self.states = {}
        self.constraints = constraints
        self.function_name = "unknown"

        # Self-assign a unique ID

        global gbl_next_uid

        self.uid = gbl_next_uid
        gbl_next_uid += 1


    def __str__(self):
        return str(self.as_dict())
        
    def as_dict(self):

        code = ""

        for instruction in self.instruction_list:
            code += str(instruction['address']) + " " + instruction['opcode']
            if instruction['opcode'].startswith("PUSH"):
                code += " " + instruction['argument']

            code += "\\n"

        return {'module_name': self.module_name, 'code': code, 'start_addr': self.start_addr, 'instruction_list': self.instruction_list, 'states': self.states, 'constraints': self.constraints}


class Edge:
    
    def __init__(self, node_from, node_to, edge_type=JumpType.UNCONDITIONAL, condition=None):

        self.node_from = node_from
        self.node_to = node_to
        self.type = edge_type
        self.condition = condition

    def __str__(self):
        return str(self.as_dict())
        
    def as_dict(self):

        return {'from': self.node_from, 'to': self.node_to}


class SVM:

    def __init__(self, modules, max_depth=MAX_DEPTH, simplify_model=True, dynamic_loader=None):
        self.modules = modules
        self.nodes = {}
        self.addr_visited = []
        self.edges = []
        self.paths = {}
        self.execution_state = {}
        self.max_depth = max_depth
        self.simplify_model = simplify_model
        self.last_call_address = None
        self.pending_returns = {}
        self.total_states = 0
        self.active_node_prefix = ""
        self.dynamic_loader = dynamic_loader

        logging.info("Initialized with dynamic loader: " + str(dynamic_loader))


    def depth_first_search(self, this_node, node_to, path, paths, depth, nodes_visited):

        if (depth > MAX_DEPTH):
            return

        if this_node == node_to:
            paths.append(path)
            return

        nodes_visited.append(this_node)

        edges_out = []

        for edge in self.edges:

            if edge.node_from == this_node and edge.node_to not in nodes_visited:
                edges_out.append(edge)

        for edge in edges_out:

            new_path = copy.deepcopy(path)
            new_path.append(edge)
            self.depth_first_search(edge.node_to, node_to, new_path, paths, depth + 1, nodes_visited)


    def find_paths(self, node_to):
        paths = []
        nodes_visited = []

        self.depth_first_search(self.active_node_prefix + ':0', node_to, [], paths, 0, nodes_visited)

        return paths


    def sym_exec(self, main_address):

        logging.debug("Starting SVM execution")

        context = Context(self.modules[main_address])

        node = self._sym_exec(context, State())
        self.nodes[node.uid] = node

        logging.info("Execution complete, saved " + str(self.total_states) + " states")

        logging.debug("--- NODES ---")
        logging.debug(self.nodes)
        logging.debug("--- EDGES ---")

        for e in self.edges:
            logging.debug(str(e.as_dict()))

        logging.info(str(len(self.nodes)) + " nodes, " + str(len(self.edges)) + " edges")
        logging.info("Resolving paths")


    def _sym_exec(self, context, state, depth=0, constraints=[]):
    
        disassembly = context.module['disassembly']
        depth = depth

        start_addr = disassembly.instruction_list[state.pc]['address']

        if start_addr == 0:
            self.execution_state['current_func'] = "main"
            self.execution_state['current_func_addr'] = start_addr

        node = Node(context.module['name'], start_addr, constraints)

        logging.info("- Entering block " + str(node.uid) + ", index = " + str(state.pc) + ", address = " + str(start_addr) + ", depth = " + str(depth))


        if start_addr in disassembly.addr_to_func:
            # Enter a new function

            function_name = disassembly.addr_to_func[start_addr]

            self.execution_state['current_func'] = function_name

            logging.info("- Entering function " + function_name)

            node.instruction_list.append({'opcode': function_name, 'address': disassembly.instruction_list[state.pc]['address']})

            state.pc += 1

        node.function_name = self.execution_state['current_func']

        halt = False

        instr = disassembly.instruction_list[state.pc]

        while not halt:

            # logging.info("- Entering instruction: " + str(disassembly.instruction_list[state.pc]))

            instr = disassembly.instruction_list[state.pc]

            node.instruction_list.append(instr)

            node.states[disassembly.instruction_list[state.pc]['address']] = copy.deepcopy(state)
            self.total_states += 1

            op = instr['opcode']

            logging.debug(op)

            # logging.debug(str(instr['address']) + " " + instr['opcode'] + str(state.stack))

            # logging.debug("[" + context.module['name'] + "] " + helper.get_trace_line(instr, state))

            state.pc += 1

            # stack ops

            if op.startswith("PUSH"):
                value = BitVecVal(int(instr['argument'][2:], 16), 256)
                state.stack.append(value)

            elif op.startswith('DUP'):
                dpth = int(op[3:])

                state.stack.append(state.stack[-dpth])

            elif op.startswith('SWAP'):

                dpth = int(op[4:])

                temp = state.stack[-dpth-1]
                state.stack[-dpth-1] = state.stack[-1]
                state.stack[-1] = temp

            elif op == 'POP':
                state.stack.pop()

            # Bitwise ops

            elif op == 'AND':
                state.stack.append(state.stack.pop() & state.stack.pop())

            elif op == 'OR':
                op1 = state.stack.pop()
                op2 = state.stack.pop()

                if (type(op1) == BoolRef):
                    op1 = If(op1, BitVecVal(1,256), BitVecVal(0,256))

                if (type(op2) == BoolRef):
                    op2 = If(op2, BitVecVal(1,256), BitVecVal(0,256))

                state.stack.append(op1 | op2)

            elif op == 'XOR':
                state.stack.append(state.stack.pop() ^ state.stack.pop())

            elif op == 'NOT':
                state.stack.append(TT256M1 - state.stack.pop())

            elif op == 'BYTE':
                s0, s1 = state.stack.pop(), state.stack.pop()  

                state.stack.append(BitVecVal(0, 256))

            # Arithmetics

            elif op == 'ADD':
                state.stack.append((helper.pop_bitvec(state) + helper.pop_bitvec(state)))

            elif op == 'SUB':
                state.stack.append((helper.pop_bitvec(state) - helper.pop_bitvec(state)))

            elif op == 'MUL':
                state.stack.append(helper.pop_bitvec(state) * helper.pop_bitvec(state))

            elif op == 'DIV':
                s0, s1 = helper.pop_bitvec(state), helper.pop_bitvec(state)

                state.stack.append(UDiv(s0, s1))

            elif op == 'MOD':
                s0, s1 = helper.pop_bitvec(state), helper.pop_bitvec(state)
                state.stack.append(0 if s1 == 0 else s0 % s1)

            elif op == 'SDIV':
                s0, s1 = helper.to_signed(helper.pop_bitvec(state)), helper.to_signed(helper.pop_bitvec(state))
                state.stack.append(0 if s1 == 0 else (abs(s0) // abs(s1) *
                                              (-1 if s0 * s1 < 0 else 1)) & TT256M1)

            elif op == 'SMOD':
                s0, s1 = helper.to_signed(helper.pop_bitvec(state)), helper.to_signed(helper.pop_bitvec(state))
                state.stack.append(0 if s1 == 0 else (abs(s0) % abs(s1) *
                                              (-1 if s0 < 0 else 1)) & TT256M1)

            elif op == 'ADDMOD':
                s0, s1, s2 = helper.pop_bitvec(state), helper.pop_bitvec(state), helper.pop_bitvec(state)
                state.stack.append((s0 + s1) % s2 if s2 else 0)

            elif op == 'MULMOD':
                s0, s1, s2 = helper.pop_bitvec(state), helper.pop_bitvec(state), helper.pop_bitvec(state)
                state.stack.append((s0 * s1) % s2 if s2 else 0)

            elif op == 'EXP':
                # we only implement 2 ** x
                base, exponent = state.stack.pop(), state.stack.pop()

                if (base.as_long() == 2):
                    if exponent == 0:
                        state.stack.append(BitVecVal(1, 256))
                    else:
                        state.stack.append(base << (exponent - 1))

                else:
                    state.stack.append(base)

            elif op == 'SIGNEXTEND':
                s0, s1 = state.stack.pop(), state.stack.pop()
                if s0 <= 31:
                    testbit = s0 * 8 + 7
                    if s1 & (1 << testbit):
                        state.stack.append(s1 | (TT256 - (1 << testbit)))
                    else:
                        state.stack.append(s1 & ((1 << testbit) - 1))
                else:
                    state.stack.append(s1)

            # Comparisons

            elif op == 'LT':

                exp = ULT(helper.pop_bitvec(state), helper.pop_bitvec(state))
                state.stack.append(exp)

            elif op == 'GT':

                exp = UGT(helper.pop_bitvec(state), helper.pop_bitvec(state))
                state.stack.append(exp)

            elif op == 'SLT':

                exp = helper.pop_bitvec(state) < helper.pop_bitvec(state)
                state.stack.append(exp)

            elif op == 'SGT':

                exp = helper.pop_bitvec(state) > helper.pop_bitvec(state)
                state.stack.append(exp)

            elif op == 'EQ':

                op1 = state.stack.pop()
                op2 = state.stack.pop()

                if(type(op1) == BoolRef):
                    op1 = If(op1, BitVecVal(1,256), BitVecVal(0,256))

                if(type(op2) == BoolRef):
                    op2 = If(op2, BitVecVal(1,256), BitVecVal(0,256))

                exp = op1 == op2

                state.stack.append(exp)

            elif op == 'ISZERO':

                val = state.stack.pop()

                if (type(val) == BoolRef):
                   exp = val == False
                else:   
                   exp = val == 0

                state.stack.append(exp)

            # Call data

            elif op == 'CALLVALUE':
                state.stack.append(context.callvalue)

            elif op == 'CALLDATALOAD':
                # unpack 32 bytes from calldata into a word and put it on the stack
                
                op0 = state.stack.pop()

                try:
                    offset = helper.get_concrete_int(simplify(op0))

                    val = b''

                    for i in range(offset, offset + 32):

                        b = context.calldata[i]
                        val += context.calldata[i].to_bytes(1, byteorder='big')

                    state.stack.append(BitVecVal(int.from_bytes(val, byteorder='big'), 256))

                except AttributeError:
                    logging.debug("CALLDATALOAD: Unsupported symbolic index or calldata value")
                    state.stack.append(BitVec("calldata_" + str(context.module['name']) + "_" + str(op0), 256))
                except IndexError:
                    logging.debug("Calldata not set, using symbolic variable instead")
                    state.stack.append(BitVec("calldata_" + str(context.module['name']) + "_" + str(op0), 256))
                                       
            elif op == 'CALLDATASIZE':

                if context.calldata_type == CalldataType.SYMBOLIC:
                    state.stack.append(BitVec("calldatasize", 256))
                else:
                    state.stack.append(BitVecVal(len(context.calldata), 256))

            elif op == 'CALLDATACOPY':
                op0, op1, op2 = state.stack.pop(), state.stack.pop(), state.stack.pop()

                try:
                    mstart = helper.get_concrete_int(op0)
                    dstart = helper.get_concrete_int(op1)
                    size = helper.get_concrete_int(op2)

                    state.mem_extend(mstart, size)

                    i_data = context.calldata[dstart]

                    for i in range(mstart, mstart + size):
                        state.memory[i] = context.calldata[i_data]
                        i_data += 1
                except:
                    # Calldata is symbolic. Do nothing
                    continue

            # Control flow

            elif op == 'STOP':
                return node

            # Environment

            elif op == 'ADDRESS':
                state.stack.append(context.address)

            elif op == 'BALANCE':
                addr = state.stack.pop()
                state.stack.append(BitVec("balance_at_" + str(addr), 256))

            elif op == 'ORIGIN':
                state.stack.append(context.origin)

            elif op == 'CALLER':
                state.stack.append(context.caller)

            elif op == 'CODESIZE':
                state.stack.append(len(disassembly.instruction_list))

            if op == 'SHA3':
                op0, op1 = state.stack.pop(), state.stack.pop()

                try:
                    index, length = helper.get_concrete_int(op0), helper.get_concrete_int(op1)

                except:
                    # Can't access symbolic memory offsets
                    state.stack.append(BitVec("KECCAC_mem_" + str(op0) + ")", 256))
                    continue

                try:
                    data = state.memory[index].as_long().to_bytes(32, byteorder='big')
                except:
                    # Can't hash symbolic values                    
                    state.stack.append(BitVec("KECCAC_mem_" + str(op0) + ")", 256))
                    continue

                logging.info("SHA3 Data: " + str(data))

                keccac = utils.sha3(utils.bytearray_to_bytestr(data))

                logging.info("Hash: " + str(keccac))

                state.stack.append(BitVecVal(int(keccac), 256))

            elif op == 'GASPRICE':
                state.stack.append(BitVecVal(1, 256))

            elif op == 'CODECOPY':
                # Not implemented
                start, s1, size = state.stack.pop(), state.stack.pop(), state.stack.pop()

            elif op == 'EXTCODESIZE':
                addr = state.stack.pop()
                state.stack.append(BitVec("extcodesize", 256))

            elif op == 'EXTCODECOPY':
                # Not implemented

                addr = state.stack.pop()
                start, s2, size = state.stack.pop(), state.stack.pop(), state.stack.pop()

            elif op == 'BLOCKHASH':
                state.stack.append(BitVec("blockhash", 256))

            elif op == 'COINBASE':
                state.stack.append(BitVec("coinbase", 256))

            elif op == 'TIMESTAMP':
                state.stack.append(BitVec("timestamp", 256))

            elif op == 'NUMBER':
                state.stack.append(BitVec("block_number", 256))

            elif op == 'DIFFICULTY':
                state.stack.append(BitVec("block_difficulty", 256))

            elif op == 'GASLIMIT':
                state.stack.append(BitVec("block_gaslimit", 256))

            elif op == 'MLOAD':
                
                op0 = state.stack.pop()

                try:
                    offset = helper.get_concrete_int(op0)
                except AttributeError:
                    logging.debug("Can't MLOAD from symbolic index")
                    data = BitVec("mem_" + str(op0), 256)
                    continue

                try:   
                    data = helper.concrete_int_from_bytes(state.memory, offset)
                except IndexError: # Memory slot not allocated
                    data = BitVec("mem_" + str(offset), 256)
                except TypeError: # Symbolic memory
                    data = state.memory[offset]

                logging.debug("Load from memory[" + str(offset) + "]: " + str(data))

                state.stack.append(data)

            elif op == 'MSTORE':

                op0, value = state.stack.pop(), state.stack.pop()

                try:
                    offset = helper.get_concrete_int(op0)
                except AttributeError:
                    logging.debug("MSTORE to symbolic index. Not supported")
                    continue

                state.mem_extend(offset, 32)

                logging.debug("MSTORE to mem[" + str(offset) + "]: " + str(value))

                try:
                    # Attempt to concretize value
                    _bytes = helper.concrete_int_to_bytes(value)

                    i = 0

                    for b in _bytes:
                        state.memory[offset + i] = _bytes[i]
                        i += 1
                except:
                    state.memory[offset] = value


            elif op == 'MSTORE8':
                # Is this ever used?
                op0, value = state.stack.pop(), state.stack.pop()

                try:
                    offset = helper.get_concrete_int(op0)
                except AttributeError:
                    logging.debug("MSTORE to symbolic index. Not supported")
                    continue

                state.mem_extend(offset, 1)

                state.memory[offset] = value % 256

            elif op == 'SLOAD':
                index = state.stack.pop()
                logging.debug("Storage access at index " + str(index))

                if type(index) == BitVecRef:
                    # SLOAD from hash offset

                    k = sha3.keccak_512()
                    k.update(bytes(str(index), 'utf-8'))
                    index = k.hexdigest()[:8]

                try:
                    data = state.storage[index]
                except KeyError:
                    data = BitVec("storage_" + str(index), 256)
                    state.storage[index] = data

                state.stack.append(data)

            elif op == 'SSTORE':
                index, value = state.stack.pop(), state.stack.pop()

                logging.debug("Write to storage[" + str(index) + "] at node " + str(start_addr))

                if type(index) == BitVecRef:
                    # SSTORE to hash offset

                    k = sha3.keccak_512()
                    k.update(bytes(str(index), 'utf-8'))
                    index = k.hexdigest()[:8]

                    state.storage[index] = value
                else:
                    index = str(index)

                try:
                    state.storage[index]
                except KeyError:
                    state.storage[index] = BitVec("storage_" + str(index), 256)

            elif op == 'JUMP':

                try:
                    jump_addr = helper.get_concrete_int(state.stack.pop())
                except AttributeError:
                    logging.info("Invalid jump argument (symbolic address) at " + str(disassembly.instruction_list[state.pc]['address']))
                    return node

                if (depth < self.max_depth):

                    i = helper.get_instruction_index(disassembly.instruction_list, jump_addr)

                    if i is None:
                        logging.info("JUMP to invalid address")
                        continue

                    opcode = disassembly.instruction_list[i]['opcode']

                    if opcode == "JUMPDEST":

                        new_state = copy.deepcopy(state)
                        new_state.pc = i

                        new_node = self._sym_exec(context, new_state, depth=depth+1, constraints=constraints)
                        self.nodes[new_node.uid] = new_node

                        self.edges.append(Edge(node.uid, new_node.uid, JumpType.UNCONDITIONAL))
                    else:
                        logging.info("Skipping JUMP to invalid destination.")

                return node

            elif op == 'JUMPI':
                op0, condition = state.stack.pop(), state.stack.pop()

                try:
                    jump_addr = helper.get_concrete_int(op0)
                except:
                    logging.info("Skipping JUMPI to invalid destination.")
                    continue

                if (depth < self.max_depth):

                    i = helper.get_instruction_index(disassembly.instruction_list, jump_addr)

                    if not i:
                        logging.debug("Invalid jump destination: " + str(jump_addr))

                    else:
                        instr = disassembly.instruction_list[i]

                        # Add new node for condition == True

                        if instr['opcode'] != "JUMPDEST":
                            logging.info("Invalid jump destination: " + str(jump_addr))

                        else:

                            # Prune unreachable destinations (if concrete values are used)

                            # logging.info("JUMPI condition: " + str(condition))

                            logging.info("CONDITION TYPE:" + str(type(condition)))

                            if (type(condition) == bool):
                                return node

                            if str(simplify(condition)) != "False":

                                if jump_addr not in self.addr_visited:

                                    self.addr_visited.append(jump_addr)

                                    new_state = copy.deepcopy(state)
                                    new_state.pc = i

                                    new_constraints = copy.deepcopy(constraints)
                                    new_constraints.append(condition)

                                    new_node = self._sym_exec(context, new_state, depth=depth+1, constraints=new_constraints)
                                    self.nodes[new_node.uid] = new_node

                                    self.edges.append(Edge(node.uid, new_node.uid, JumpType.CONDITIONAL, condition))

                        #if not self.simplify_model:

                        new_state = copy.deepcopy(state)

                        if (type(condition) == BoolRef):
                            negated = Not(condition)
                        else:
                            negated = condition == 0

                        new_constraints = copy.deepcopy(constraints)
                        new_constraints.append(negated)

                        new_node = self._sym_exec(context, new_state, depth=depth, constraints=new_constraints)
                        self.nodes[new_node.uid] = new_node

                        self.edges.append(Edge(node.uid, new_node.uid, JumpType.CONDITIONAL, negated))

                        return node


            elif op == 'PC':
                state.stack.append(state.pc - 1)

            elif op == 'MSIZE':
                state.stack.append(BitVec("msize", 256))

            elif op == 'GAS':
                state.stack.append(10000000)

            elif op.startswith('LOG'):
                dpth = int(op[3:])
                state.stack.pop(), state.stack.pop()
                [state.stack.pop() for x in range(dpth)]
                # Not supported

            elif op == 'CREATE':
                state.stack.pop(), state.stack.pop(), state.stack.pop()
                # Not supported
                state.stack.append(0)

            elif op in ('CALL', 'CALLCODE', 'DELEGATECALL', 'STATICCALL'):
                if op in ('CALL', 'CALLCODE'):
                    gas, to, value, meminstart, meminsz, memoutstart, memoutsz = \
                        state.stack.pop(), state.stack.pop(), state.stack.pop(), state.stack.pop(), state.stack.pop(), state.stack.pop(), state.stack.pop()
                else:
                    gas, to, meminstart, meminsz, memoutstart, memoutsz = \
                        state.stack.pop(), state.stack.pop(), state.stack.pop(), state.stack.pop(), state.stack.pop(), state.stack.pop()

                try:
                    callee_address = hex(helper.get_concrete_int(to))
                    module = self.modules[callee_address]
                except AttributeError:
                    logging.info("Unable to get concrete call address")
                    if self.dynamic_loader is not None:

                        logging.info("Attempting to resolve dependency")
                        module = self.dynamic_loader.dynld(context.module['address'], str(simplify(to)))

                        if module is None:

                            logging.info("No contract code returned, not a contract account?")
                            ret = BitVec("retval_" + str(disassembly.instruction_list[state.pc]['address']), 256)
                            state.stack.append(ret)

                            continue

                    else:
                        logging.info("Dynamic loader unavailable. Skipping call")
                        ret = BitVec("retval_" + str(disassembly.instruction_list[state.pc]['address']), 256)
                        state.stack.append(ret)

                        continue
                except KeyError:
                    logging.info("Module with address " + callee_address + " not loaded.")

                    if self.dynamic_loader is not None:

                        logging.info("Attempting to load dependency")

                        module = self.dynamic_loader.dynld(context.module['address'], callee_address)

                        if module is None:

                            logging.info("No  code returned, not a contract account?")
                            ret = BitVec("retval_" + str(disassembly.instruction_list[state.pc]['address']), 256)
                            state.stack.append(ret)
                            continue

                    else:
                         logging.info("Dynamic loader unavailable. Skipping call")
                         ret = BitVec("retval_" + str(disassembly.instruction_list[state.pc]['address']), 256)
                         state.stack.append(ret)
                         continue


                callee_address = module['address']
                self.modules[callee_address] = module    
                logging.info(op + " to: " + callee_address)

                try:
                    callee_module = self.modules[callee_address]
                except KeyError:
                    logging.info("Contract " + str(callee_address) + " not loaded.")
                    logging.info((str(self.modules)))
                    
                    ret = BitVec("retval_" + str(disassembly.instruction_list[state.pc]['address']), 256)
                    state.stack.append(ret)

                    continue

                # Attempt to write concrete calldata

                logging.info("Meminstart: " + str(meminstart) + ", meminsize : " + str(meminsz))

                try:
                    calldata = state.memory[helper.get_concrete_int(meminstart):helper.get_concrete_int(meminstart+meminsz)]
                    calldata_type = CalldataType.CONCRETE
                    logging.info("calldata: " + str(calldata))

                except AttributeError:

                    logging.info("Unsupported symbolic calldata offset")
                    calldata_type = CalldataType.SYMBOLIC
                    calldata = []

                self.last_call_address = disassembly.instruction_list[state.pc]['address']
                self.pending_returns[self.last_call_address] = []

                callee_context = Context(callee_module, calldata = calldata, caller = context.address, origin = context.origin, calldata_type = calldata_type)

                if (op == 'CALL'):

                    new_node = self._sym_exec(callee_context, State(), depth=depth+1, constraints=constraints)
                    self.nodes[new_node.uid] = new_node

                elif (op == 'CALLCODE'):

                    temp_code = context.module['disassembly']
                    temp_value = context.value
                    temp_caller = context.caller
                    temp_calldata = context.calldata

                    context.module['disassembly']  = callee_module['disassembly']
                    context.value = value
                    context.caller = context.address
                    context.calldata = calldata

                    new_node = self._sym_exec(callee_context, State(), depth=depth+1, constraints=constraints)
                    self.nodes[new_node.uid] = new_node

                    context.module['disassembly'] = temp_code
                    context.value = temp_value
                    context.caller = temp_caller
                    context.calldata = temp_calldata

                elif (op == 'DELEGATECALL'):

                    temp_code = context.module['disassembly']
                    temp_calldata = context.calldata

                    context.module['disassembly'] = callee_module['disassembly']
                    context.calldata = calldata

                    new_node = self._sym_exec(callee_context, State(), depth=depth + 1, constraints=constraints)
                    self.nodes[new_node.uid] = new_node

                    context.module['disassembly'] = temp_code
                    context.calldata = temp_calldata

                self.edges.append(Edge(node.uid, new_node.uid, JumpType.CALL))

                ret = BitVec("retval_" + str(disassembly.instruction_list[state.pc]['address']), 256)
                state.stack.append(ret)

                new_state = copy.deepcopy(state)

                # memoutstart = helper.get_concrete_int(memoutstart)
                # memoutsz = helper.get_concrete_int(memoutsz

                # new_state.mem_extend(memoutstart, memoutsz)
                # new_state.memory[memoutstart:memoutstart + memoutsz] = self.last_returned

                new_node = self._sym_exec(context, new_state, depth=depth+1, constraints=constraints)

                self.nodes[new_node.uid] = new_node

                for ret_uid in self.pending_returns[self.last_call_address]:
                    self.edges.append(Edge(ret_uid, new_node.uid, JumpType.RETURN))

                return node

            elif op == 'RETURN':
                offset, length = state.stack.pop(), state.stack.pop()

                try:
                    self.last_returned = state.memory[helper.get_concrete_int(offset):helper.get_concrete_int(offset+length)]
                except AttributeError:
                    logging.debug("Return with symbolic length or offset. Not supported")

                if self.last_call_address is not None:
                    self.pending_returns[self.last_call_address].append(node.uid)

                return node

            elif op == 'SUICIDE':
                return node

            elif op == 'REVERT':
                return node

            elif op == 'INVALID':
                return node

