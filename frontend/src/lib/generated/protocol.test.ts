import { describe, expect, it } from 'vitest';
import { GatewayOp, PROTOCOL_VERSION } from './ops';
import { Permission } from './permissions';

describe('generated protocol constants', () => {
  it('uses protocol v1 and stable opcodes', () => {
    expect(PROTOCOL_VERSION).toBe(1);
    expect(GatewayOp.IDENTIFY).toBe(2);
    expect(Permission.MODERATE_MEMBERS).toBe(1n << 40n);
  });
});
