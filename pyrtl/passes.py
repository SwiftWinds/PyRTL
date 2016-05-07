
"""
Passes contains structures helpful for writing analysis and
transformation passes over blocks.
"""

from __future__ import print_function, unicode_literals

from .core import working_block, set_working_block, debug_mode, LogicNet, PostSynthBlock
from .helperfuncs import (
    find_and_print_loop, as_wires, concat_list,
    _basic_mult, _basic_add, _basic_sub, _basic_select, _basic_eq, _basic_lt, _basic_gt)
from .memory import MemBlock
from .pyrtlexceptions import PyrtlError, PyrtlInternalError
from .wire import WireVector, Input, Output, Const, Register
from .transform import net_transform, _get_new_block_mem_instance, copy_block


# --------------------------------------------------------------------
#   __   __  ___           __      ___    __
#  /  \ |__)  |  |  |\/| |  /  /\   |  | /  \ |\ |
#  \__/ |     |  |  |  | | /_ /~~\  |  | \__/ | \|
#


def optimize(update_working_block=True, block=None, skip_sanity_check=False):
    """ Return an optimized version of a synthesized hardware block. """
    block = working_block(block)
    if not update_working_block:
        block = copy_block(block)

    if (not skip_sanity_check) or debug_mode:
        block.sanity_check()
    _remove_wire_nets(block)
    if debug_mode:
        block.sanity_check()
    _constant_propagation(block)
    if debug_mode:
        block.sanity_check()
    _remove_unlistened_nets(block)
    if (not skip_sanity_check) or debug_mode:
        block.sanity_check()
    return block


def _remove_wire_nets(block):
    """ Remove all wire nodes from the block. """

    def find_producer(x):
        # trace back to the root producer of x
        if x in immediate_producer:
            return find_producer(immediate_producer[x])
        else:
            return x

    immediate_producer = {}  # map from wirevector to its direct producer wirevector
    wire_removal_set = set()  # set of all wirevectors to be removed

    # one pass to build the map of value producers and
    # all of the nets and wires to be removed
    for net in block.logic:
        if net.op == 'w':
            immediate_producer[net.dests[0]] = net.args[0]
            if not isinstance(net.dests[0], Output):
                wire_removal_set.add(net.dests[0])

    # second full pass to create the new logic without the wire nets
    new_logic = set()
    for net in block.logic:
        if net.op != 'w' or isinstance(net.dests[0], Output):
            new_args = tuple(find_producer(x) for x in net.args)
            new_net = LogicNet(net.op, net.op_param, new_args, net.dests)
            new_logic.add(new_net)

    # now update the block with the new logic and remove wirevectors
    block.logic = new_logic
    for dead_wirevector in wire_removal_set:
        del block.wirevector_by_name[dead_wirevector.name]
        block.wirevector_set.remove(dead_wirevector)

    block.sanity_check()


def _constant_propagation(block, silence_unexpected_net_warnings=False):
    """ Removes excess constants in the block.

    Note on resulting block:
    The output of the block can have wirevectors that are driven but not
    listened to. This is to be expected. These are to be removed by the
    _remove_unlistened_nets function
    """

    current_nets = 0
    while len(block.logic) != current_nets:
        current_nets = len(block.logic)
        _constant_prop_pass(block, silence_unexpected_net_warnings)


def _constant_prop_pass(block, silence_unexpected_net_warnings=False):
    """ Does one constant propagation pass """
    def _constant_prop_error(net, error_str):
        if not silence_unexpected_net_warnings:
            raise PyrtlError("Unexpected net, {}, has {}".format(net, error_str))

    valid_net_ops = '~&|^nrwcsm@'
    no_optimization_ops = 'wcsm@'

    def constant_prop_check(net_checking):

        def replace_net(new_net):
            nets_to_remove.add(net_checking)
            nets_to_add.add(new_net)

        def replace_net_with_const(const_val):
            new_const_wire = Const(bitwidth=1, val=const_val, block=block)
            wire_add_set.add(new_const_wire)
            replace_net_with_wire(new_const_wire)

        def replace_net_with_wire(new_wire):
            if isinstance(net_checking.dests[0], Output):
                replace_net(LogicNet('w', None, args=(new_wire,),
                                     dests=net_checking.dests))
            else:
                nets_to_remove.add(net_checking)
                replacement_wires[net_checking.dests[0]] = new_wire

        one_var_ops = {
            '~': lambda x: 1-x,
            'r': lambda x: x   # This is only valid for constant folding purposes
        }
        two_var_ops = {
            '&': lambda l, r: l & r,
            '|': lambda l, r: l | r,
            '^': lambda l, r: l ^ r,
            'n': lambda l, r: 1-(l & r),
        }
        num_constants = sum((isinstance(arg_wire, Const)
                            for arg_wire in net_checking.args))

        if net_checking.op not in valid_net_ops:
            _constant_prop_error(net_checking, "has a net not handled by constant_propagation")
            return  # skip if we are ignoring unoptimizable ops

        if num_constants is 0 or net_checking.op in no_optimization_ops:
            return  # assuming wire nets are already optimized

        if any(len(wire) != 1 for wire in net_checking.args + net_checking.dests):
            long_wires = [wire for wire in net_checking.args + net_checking.dests if
                          len(wire) != 1]
            _constant_prop_error(net_checking, "has wire(s) {} with bitwidths that are not 1"
                                 .format(long_wires))
            return  # skip if we are ignoring unoptimizable ops

        if (net_checking.op in two_var_ops) and num_constants == 1:
            # special case
            const_wire, other_wire = net_checking.args
            if isinstance(other_wire, Const):
                const_wire, other_wire = other_wire, const_wire

            outputs = [two_var_ops[net_checking.op](const_wire.val, other_val)
                       for other_val in (0, 1)]

            if outputs[0] == outputs[1]:
                replace_net_with_const(outputs[0])
            elif outputs[0] == 0:
                replace_net_with_wire(other_wire)
            else:
                replace_net(LogicNet('~', None, args=(other_wire,),
                                     dests=net_checking.dests))

        else:
            if net_checking.op in two_var_ops:
                output = two_var_ops[net_checking.op](net_checking.args[0].val,
                                                      net_checking.args[1].val)
            elif net_checking.op in one_var_ops:
                output = one_var_ops[net_checking.op](net_checking.args[0].val)
            else:
                # this is for nets that we are not modifying (eg spliting, and memory)
                return
            replace_net_with_const(output)

    def find_producer(x):
        # trace back to the root producer of x
        if x in replacement_wires:
            return find_producer(replacement_wires[x])
        else:
            return x

    replacement_wires = {}  # map from wire to its producer
    wire_add_set = set()
    nets_to_add = set()
    nets_to_remove = set()

    for a_net in block.logic:
        constant_prop_check(a_net)
    # second full pass to cleanup

    new_logic = set()
    for net in block.logic.union(nets_to_add) - nets_to_remove:
        new_args = tuple(find_producer(x) for x in net.args)
        new_net = LogicNet(net.op, net.op_param, new_args, net.dests)
        new_logic.add(new_net)

    block.logic = new_logic
    for new_wirevector in wire_add_set:
        block.add_wirevector(new_wirevector)

    _remove_unused_wires(block)


def _remove_unlistened_nets(block):
    """ Removes all nets that are not connected to an output wirevector
    """

    listened_nets = set()
    listened_wires = set()
    prev_listened_net_count = 0

    def add_to_listened(net):
        listened_nets.add(net)
        listened_wires.update(net.args)

    for a_net in block.logic:
        if a_net.op == '@':
            add_to_listened(a_net)
        elif any(isinstance(destW, Output) for destW in a_net.dests):
            add_to_listened(a_net)

    while len(listened_nets) > prev_listened_net_count:
        prev_listened_net_count = len(listened_nets)

        for net in block.logic - listened_nets:
            if any((destWire in listened_wires) for destWire in net.dests):
                add_to_listened(net)

    block.logic = listened_nets
    _remove_unused_wires(block)


def _remove_unused_wires(block, keep_inputs=True):
    """ Removes all unconnected wires from a block"""
    valid_wires = set()
    for logic_net in block.logic:
        valid_wires.update(logic_net.args, logic_net.dests)

    wire_removal_set = block.wirevector_set.difference(valid_wires)
    for removed_wire in wire_removal_set:
        if isinstance(removed_wire, Input):
            term = " optimized away"
            if keep_inputs:
                valid_wires.add(removed_wire)
                term = " deemed useless by optimization"

            print("Input Wire, " + removed_wire.name + " has been" + term)
        if isinstance(removed_wire, Output):
            PyrtlInternalError("Output wire, " + removed_wire.name + " not driven")

    block.wirevector_set = valid_wires

# --------------------------------------------------------------------
#    __           ___       ___  __     __
#   /__` \ / |\ |  |  |__| |__  /__` | /__`
#   .__/  |  | \|  |  |  | |___ .__/ | .__/
#


def synthesize(update_working_block=True, block=None):
    """ Lower the design to just single-bit "and", "or", and "not" gates.

    :param update_working_block: Boolean specifying if working block update
    :param block: The block you want to synthesize
    :return: The newly synthesized block (of type PostSynthesisBlock).

    Takes as input a block (default to working block) and creates a new
    block which is identical in function but uses only single bit gates
    and excludes many of the more complicated primitives.  The new block
    should consist *almost* exclusively of the combination elements
    of w, &, |, ^, and ~ and sequential elements of registers (which are
    one bit as well).  The two exceptions are for inputs/outputs (so that
    we can keep the same interface) which are immediately broken down into
    the individual bits and memories.  Memories (read and write ports) which
    require the reassembly and disassembly of the wirevectors immediately
    before and after.  There arethe only two places where 'c' and 's' ops
    should exist.

    The block that results from synthesis is actually of type
    "PostSynthesisBlock" which contains a mapping from the original inputs
    and outputs to the inputs and outputs of this block.  This is used during
    simulation to map the input/outputs so that the same testbench can be
    used both pre and post synthesis (see documentation for Simulation for
    more details).
    """

    block_pre = working_block(block)
    block_pre.sanity_check()  # before going further, make sure that pressynth is valid
    block_in = copy_block(block_pre, update_working_block=False)

    block_out = PostSynthBlock()
    # resulting block should only have one of a restricted set of net ops
    block_out.legal_ops = set('~&|^nrwcsm@')
    wirevector_map = {}  # map from (vector,index) -> new_wire
    rev_io_map = {v: k for k, v in block_in.io_map.items()}  # pylint: disable=no-member
    io_map = block_out.io_map  # map from presynth inputs and outputs to postsynth i/o
    uid = 0  # used for unique names

    with set_working_block(block_out):
        # First, replace advanced operators with simpler ones
        for op, fun in [
                ('*', _basic_mult),
                ('+', _basic_add),
                ('-', _basic_sub),
                ('x', _basic_select),
                ('=', _basic_eq),
                ('<', _basic_lt),
                ('>', _basic_gt)]:
            net_transform(_replace_op(op, fun), block_in)

        # Next, create all of the new wires for the new block
        # from the original wires and store them in the wirevector_map
        # for reference.
        for wirevector in block_in.wirevector_subset():
            for i in range(len(wirevector)):
                new_name = '_'.join((wirevector.name, 'synth', str(i), str(uid)))
                uid += 1
                if isinstance(wirevector, Const):
                    new_val = (wirevector.val >> i) & 0x1
                    new_wirevector = Const(bitwidth=1, val=new_val)
                elif isinstance(wirevector, (Input, Output)):
                    new_wirevector = WireVector(name="tmp_" + new_name, bitwidth=1)
                else:
                    new_wirevector = wirevector.__class__(name=new_name, bitwidth=1)
                wirevector_map[(wirevector, i)] = new_wirevector

        # Now connect up the inputs and outputs to maintain the interface
        for wirevector in block_in.wirevector_subset(Input):
            input_vector = Input(
                name=rev_io_map[wirevector].name,
                bitwidth=len(wirevector),
                block=block_out)
            io_map[rev_io_map[wirevector]] = input_vector
            for i in range(len(wirevector)):
                wirevector_map[(wirevector, i)] <<= input_vector[i]
        for wirevector in block_in.wirevector_subset(Output):
            output_vector = Output(
                name=rev_io_map[wirevector].name,
                bitwidth=len(wirevector),
                block=block_out)
            io_map[rev_io_map[wirevector]] = output_vector
            # the "reversed" is needed because most significant bit comes first in concat
            output_bits = [wirevector_map[(wirevector, i)]
                           for i in range(len(output_vector))]
            output_vector <<= concat_list(output_bits)

        # Now that we have all the wires built and mapped, walk all the blocks
        # and map the logic to the equivalent set of primitives in the system
        out_mems = block_out.mem_map  # dictionary: PreSynth Map -> PostSynth Map
        for net in block_in.logic:
            _decompose(net, wirevector_map, out_mems, block_out)

    if update_working_block:
        set_working_block(block_out)
    return block_out


def _replace_op(op, fun):
    def _replace_op_inner(net):
        if net.op != op:
            return True
        dest = net.dests[0]
        dest <<= fun(*net.args)
        return False
    return _replace_op_inner


def _decompose(net, wv_map, mems, block_out):
    """ Add the wires and logicnets to block_out and wv_map to decompose net """

    def arg(x, i):
        # return the mapped wire vector for argument x, wire number i
        return wv_map[(net.args[x], i)]

    def destlen():
        # return iterator over length of the destination in bits
        return range(len(net.dests[0]))

    def assign_dest(i, v):
        # assign v to the wiremap for dest[0], wire i
        wv_map[(net.dests[0], i)] <<= v

    one_var_ops = {
        'w': lambda w: w,
        '~': lambda w: ~w,
    }
    c_two_var_ops = {
        '&': lambda l, r: l & r,
        '|': lambda l, r: l | r,
        '^': lambda l, r: l ^ r,
        'n': lambda l, r: l.nand(r),
    }

    if net.op in one_var_ops:
        for i in destlen():
            assign_dest(i, one_var_ops[net.op](arg(0, i)))
    elif net.op in c_two_var_ops:
        for i in destlen():
            assign_dest(i, c_two_var_ops[net.op](arg(0, i), arg(1, i)))
    elif net.op == 's':
        for i in destlen():
            selected_bit = arg(0, net.op_param[i])
            assign_dest(i, selected_bit)
    elif net.op == 'c':
        arg_wirelist = []
        # generate list of wires for vectors being concatenated
        for arg_vector in net.args:
            arg_vector_as_list = [wv_map[(arg_vector, i)] for i in range(len(arg_vector))]
            arg_wirelist = arg_vector_as_list + arg_wirelist
        for i in destlen():
            assign_dest(i, arg_wirelist[i])
    elif net.op == 'r':
        for i in destlen():
            args = (arg(0, i),)
            dests = (wv_map[(net.dests[0], i)],)
            new_net = LogicNet('r', None, args=args, dests=dests)
            block_out.add_net(new_net)
    elif net.op == 'm':
        arg0list = [arg(0, i) for i in range(len(net.args[0]))]
        addr = concat_list(arg0list)
        new_mem = _get_new_block_mem_instance(net.op_param, mems, block_out)[1]
        data = as_wires(new_mem[addr])
        for i in destlen():
            assign_dest(i, data[i])
    elif net.op == '@':
        addrlist = [arg(0, i) for i in range(len(net.args[0]))]
        addr = concat_list(addrlist)
        datalist = [arg(1, i) for i in range(len(net.args[1]))]
        data = concat_list(datalist)
        enable = arg(2, 0)
        new_mem = _get_new_block_mem_instance(net.op_param, mems, block_out)[1]
        new_mem[addr] <<= MemBlock.EnabledWrite(data=data, enable=enable)
    else:
        raise PyrtlInternalError('Unable to synthesize the following net '
                                 'due to unimplemented op :\n%s' % str(net))
    return


def nand_synth(block=None):
    """
    Synthesizes an Post-Synthesis block into one consisting of nands and inverters in place
    :param block: The block to synthesize.
    """
    def nand_synth_op(net):
        if net.op in '~nrwcsm@':
            return True

        def arg(num):
            return net.args[num]

        dest = net.dests[0]
        if net.op == '&':
            dest <<= ~(arg(0).nand(arg(1)))
        elif net.op == '|':
            dest <<= (~arg(0)).nand(~arg(1))
        elif net.op == '^':
            temp_0 = arg(0).nand(arg(1))
            dest <<= temp_0.nand(arg(0)).nand(temp_0.nand(arg(1)))
        else:
            raise PyrtlError("Op, '{}' is not supported in nand_synth".format(net.op))

    net_transform(nand_synth_op, block)


def and_inverter_synth(block=None):
    """
    Transforms a decomposed block into one consisting of ands and inverters in place
    :param block: The block to synthesize
    """
    def and_inv_op(net):
        if net.op in '~&rwcsm@':
            return True

        def arg(num):
            return net.args[num]

        dest = net.dests[0]
        if net.op == '|':
            dest <<= ~(~arg(0) & ~arg(1))
        elif net.op == '^':
            all_1 = arg(0) & arg(1)
            all_0 = ~arg(0) & ~arg(1)
            dest <<= all_0 & ~all_1
        elif net.op == 'n':
            dest <<= ~(arg(0) & arg(1))
        else:
            raise PyrtlError("Op, '{}' is not supported in and_inv_synth".format(net.op))

    net_transform(and_inv_op, block)
