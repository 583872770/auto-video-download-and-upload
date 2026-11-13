import { bootstrap } from '../bootstrap';
import { ApiClient } from './client';

export const api = new ApiClient(() => bootstrap().csrf);
